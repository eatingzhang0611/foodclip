#!/usr/bin/env python3
"""
foodclip 的 HTTP 入口，给 iOS 快捷指令用。

鉴权用「不可猜的路径」而不是 token 校验：URL 本身就是凭证，
省掉一整套 401 处理，快捷指令和浏览器书签各存一次就永久可用。
路径从 .env 的 SECRET 读，没有就自动生成一个写回去。

    POST /<SECRET>/add   body 是分享文案 → 跑完返回 JSON
    GET  /<SECRET>/      → index.html
"""

import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import foodclip  # noqa: E402

HERE = Path(__file__).parent
PORT = int(os.environ.get("PORT", 8788))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
# 跨国传图+推理一条要 2~5 分钟，串行就够个人用，也顺便避开 shops.jsonl 的写竞态
LOCK = threading.Lock()


def get_secret():
    s = os.environ.get("SECRET")
    if s:
        return s
    s = secrets.token_urlsafe(24)
    env = HERE / ".env"
    env.write_text((env.read_text() if env.exists() else "") + f"\nSECRET={s}\n")
    print(f"已生成 SECRET 并写入 .env: {s}", file=sys.stderr)
    return s


SECRET = get_secret()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        body = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _path(self):
        """校验前缀，返回剩余路径；不匹配返回 None。"""
        p = self.path.split("?")[0]
        return p[len(SECRET) + 1:] or "/" if p.startswith(f"/{SECRET}") else None

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, "ok", "text/plain")
        rest = self._path()
        if rest is None:
            return self._send(404, "", "text/plain")
        if rest in ("/", ""):
            f = foodclip.OUT / "index.html"
            if not f.exists():
                return self._send(200, "<meta charset=utf-8>还没有数据，先分享一条进来",
                                  "text/html")
            return self._send(200, f.read_bytes(), "text/html")
        return self._send(404, "", "text/plain")

    def do_POST(self):
        rest = self._path()
        if rest == "/add":
            return self._handle_add()
        if rest == "/remove":
            return self._handle_remove()
        return self._send(404, "", "text/plain")

    def _handle_add(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8", "ignore")
        if not raw.strip():
            return self._send(400, json.dumps({"ok": False, "msg": "空请求"}))

        # 跨国传图+推理要 2~5 分钟，快捷指令等不了那么久会先断连。
        # 所以立刻回一个"已收到"，真正处理丢到后台线程，跑完用 ntfy 推送结果。
        threading.Thread(target=self._work, args=(raw,), daemon=True).start()
        self._send(200, json.dumps(
            {"ok": True, "msg": "已收到，后台处理中（1~5分钟），完成后推送通知", "queued": True},
            ensure_ascii=False))

    def _handle_remove(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8", "ignore")
        try:
            k = json.loads(raw).get("k", "")
        except Exception:
            k = ""
        if not k:
            return self._send(400, json.dumps({"ok": False, "msg": "缺 k"}))

        with LOCK:
            removed = foodclip.remove_shop(k)
            if removed:
                foodclip.render_index(os.environ.get("CITY", ""))
        self._send(200, json.dumps({"ok": True, "removed": removed}, ensure_ascii=False))

    def _work(self, raw):
        list_url = f"{PUBLIC_URL}/{SECRET}/" if PUBLIC_URL else None
        with LOCK:
            try:
                r = foodclip.process(raw)
            except Exception as e:
                self.log_message("failed: %s", e)
                foodclip.ntfy_push("foodclip 识别失败", f"{type(e).__name__}: {e}", list_url)
                return

        msg = f"{r['author']} · {r['count']} 家"
        if r["claimed"] and r["claimed"] != r["count"]:
            msg += f"（文案称 {r['claimed']}）"
        if r["dup"]:
            msg += f" · {len(r['dup'])} 家之前见过"
        foodclip.ntfy_push("foodclip 识别完成", msg, list_url)

    def log_message(self, fmt, *args):
        print(f"· {fmt % args}", file=sys.stderr)


if __name__ == "__main__":
    print(f"listening on :{PORT}  path prefix /{SECRET}/", file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
