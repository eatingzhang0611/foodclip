#!/usr/bin/env python3
"""
foodclip — 抖音美食视频 → 店铺清单 → 大众点评深链

用法:
    python3 foodclip.py "7.94 复制打开抖音... https://v.douyin.com/xxx/ ..."

环境变量:
    LLM_BASE_URL  OpenAI 兼容接口地址，如 https://ark.cn-beijing.volces.com/api/v3
    LLM_API_KEY   密钥
    LLM_MODEL     视觉模型名

设计约束: 视频不落盘，ffmpeg 直读远程 URL 输出到管道；只持久化 shops.jsonl。
"""

import base64
import gzip
import http.cookiejar
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
COMMON_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}
# 每条请求都是孤立的裸调用，没有 cookie/会话，跟真实浏览器的连续访问模式差得太远——
# 用同一个 cookiejar 串起一次"访问"，尽量贴近真实浏览器行为。
COOKIEJAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(COOKIEJAR))
HERE = Path(__file__).parent
OUT = HERE / "out"
STORE = HERE / "shops.jsonl"

# 读 .env，不引入 python-dotenv
for line in (HERE / ".env").read_text().splitlines() if (HERE / ".env").exists() else []:
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("\"'"))

# 采样间隔。定在 2 秒是实测出来的拐点，不是拍脑袋：
#   4 秒 — 店名 6/6，但漏掉字幕里的分店信息（一句字幕只显示 2~3 秒，采样跨过去了）
#   2 秒 — 店名 6/6 + 抓到「纯味斑鱼府是连锁，选就近的」，12k token
#   1 秒 — 结果与 2 秒完全一致，token 翻倍到 22k，纯浪费
# 标题卡能撑 5~10 秒所以 4 秒就够，但细节藏在转瞬即逝的字幕里。
FRAME_INTERVAL = 2
TILE = "4x4"


def fetch(url, headers=None, retries=2):
    """VPS 到国内（豆包和抖音 CDN 都在国内）的链路本来就不稳，握手偶尔会卡过
    20 秒——重试一次通常就过了，不重试就是让一次网络抖动砸掉整条链路。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, **COMMON_HEADERS, **(headers or {})})
    for attempt in range(retries + 1):
        try:
            with OPENER.open(req, timeout=20) as r:
                body = r.read()
            return gzip.decompress(body) if r.headers.get("Content-Encoding") == "gzip" else body
        except Exception:
            if attempt == retries:
                raise
            time.sleep(1.5 * (attempt + 1))


def compress(data, max_w=960, q=4):
    """图文帖原图不经这一步传一张动辄 1~2MB 的点评卡截图，纯粹是浪费 token——
    LLM 现在是本地回环（sub2api），带宽不是瓶颈了，这里只为省 token 而不是抢救超时。"""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
           "-vf", f"scale='min(iw,{max_w})':-2", "-q:v", str(q),
           "-f", "image2", "-vcodec", "mjpeg", "pipe:1"]
    return subprocess.run(cmd, input=data, capture_output=True, check=True).stdout


def ntfy_push(title, body, click=None):
    """处理一条要 2~5 分钟（跨国传图+推理），快捷指令早就断连了，靠 ntfy 推送
    把真实结果送回手机。没配就静默跳过——不是核心链路，不能让它挡住主流程。

    走 JSON 发布接口而不是 Header 方式：中文标题/正文放 Header 会因为非 ASCII
    直接抛异常，JSON body 走 UTF-8 没这个限制。"""
    server, topic = os.environ.get("NTFY_SERVER"), os.environ.get("NTFY_TOPIC")
    if not (server and topic):
        return
    payload = {"topic": topic, "title": title, "message": body}
    if click:
        payload["click"] = click
    req = urllib.request.Request(
        f"{server.rstrip('/')}/", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        print(f"· ntfy 推送失败: {e}", file=sys.stderr)


def parse_douyin(text):
    """从分享文案里提取短链，跟重定向拿 item_id，再取元数据。

    抖音有两种内容：视频走 /share/video/，图文走 /share/note/。图文的
    video.play_addr 是个指向静音 mp3 的假地址，duration=0，喂给 ffmpeg 必挂。
    """
    m = re.search(r"https?://v\.douyin\.com/\S+", text)
    if not m:
        raise ValueError("没找到抖音短链")

    # 先摸一次首页拿基础 cookie，让后面的请求挂在同一个会话上，
    # 而不是孤零零一条裸调用——更接近真实浏览器的访问模式。
    fetch("https://www.douyin.com/", {"Referer": "https://www.douyin.com/"})
    time.sleep(random.uniform(0.4, 1.0))

    req = urllib.request.Request(m.group(0), headers={"User-Agent": UA, **COMMON_HEADERS})
    with OPENER.open(req, timeout=20) as r:
        kind, item_id = re.search(r"/(video|note|slides)/(\d+)", r.geturl()).groups()

    time.sleep(random.uniform(0.4, 1.0))
    kind = "note" if kind == "note" else "video"
    html = fetch(f"https://www.iesdouyin.com/share/{kind}/{item_id}/?region=CN&from_aid=1128",
                 {"Referer": "https://www.douyin.com/"}).decode("utf-8", "ignore")
    data = json.loads(re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\});?\s*</script>", html, re.S).group(1))
    # loaderData 的 key 随内容类型变（video_(id)/page、note_(id)/page），别写死
    try:
        page = next(v for v in data["loaderData"].values() if isinstance(v, dict) and "videoInfoRes" in v)
    except StopIteration:
        raise ValueError(f"页面结构没有 videoInfoRes，loaderData keys={list(data['loaderData'].keys())}")
    item = page["videoInfoRes"]["item_list"][0]

    images = [im["url_list"][0] for im in (item.get("images") or [])]
    return {
        "item_id": item_id,
        "kind": "note" if images else "video",
        "desc": item["desc"],
        "author": item["author"]["nickname"],
        "duration": (item.get("video") or {}).get("duration", 0) // 1000,
        "images": images,
        "video_url": (item.get("video") or {}).get("play_addr", {}).get("url_list", [None])[0],
        "share_url": f"https://www.douyin.com/{kind}/{item_id}",
    }


def grab_sheets(video_url, interval=None):
    """ffmpeg 直读远程 URL,抽帧拼图,JPEG 流走管道。视频一个字节都不落盘。"""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-user_agent", UA, "-headers", "Referer: https://www.douyin.com/\r\n",
        "-i", video_url,
        "-vf", f"fps=1/{interval or FRAME_INTERVAL},scale=360:-2,tile={TILE}",
        "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "-",
    ]
    blob = subprocess.run(cmd, capture_output=True, check=True).stdout
    # image2pipe 输出连续 JPEG,按 SOI/EOI 切开
    return [blob[i:j + 2] for i, j in
            zip([m.start() for m in re.finditer(b"\xff\xd8\xff", blob)],
                [m.start() for m in re.finditer(b"\xff\xd9", blob)])]


PROMPT = """这是一条美食推荐内容的画面，按时间/顺序排列。可能是两种形态之一：

A. 视频截图拼图 —— 有黄色描边的编号标题卡（如「01.店名」），底部有烧录字幕
B. 图文帖的原图 —— 常常直接是**大众点评/美团的店铺卡片截图**，带评分、评论数、人均

如果是 B，把卡片标题拆开：括号外的主名放 name，括号里的分店放 branch
（例：「La Mia Casa漫味意式小馆（上海路店）」→ name="La Mia Casa漫味意式小馆"，
  branch="上海路店"。**name 里不要再带括号，会污染搜索关键词**）

输出 JSON 对象：
{"shops": [{"no": 序号, "name": "店名", "note": "一句话，25字内",
            "branch": 分店限定 或 null}]}

note 写什么：
- 画面里有点评卡片时，**优先抄客观数据**：「4.8分 ¥122/人 意大利菜」
- 没有客观数据时，才写主观特色：「烤三文鱼头，需提前预约」
- 不要写「吃意大利菜来这」这种把图上标语照搬的废话，那是标题不是信息

branch 字段规则（这条最容易做错，看清楚）：
- 字幕里只要出现**连锁、分店、就近、哪一家、总店、老店、某某路/某某广场店**这类说法，
  就要填。照抄原话的说法，别自己概括：
    「一定要去愚园路那家老店」   → "愚园路老店"
    「这家店就是个连锁店了，大家可以选自己就近的」 → "连锁不限"
    「他家有好几家，我推荐静安寺那家」 → "静安寺店"
- **视频里完全没提到分店这回事，才填 null。不要根据店名猜，不要补全。**
  「东京屋」你无法从名字知道它有没有分店——那就是 null，不是你该判断的事

严格要求：
- **只收录画面里能看到店名的店**（视频看标题卡，图文看卡片标题）。
  文案里提到但画面里没有的，也收——文案是作者亲手打的，可信度高于画面推断
- 文案和画面重复的同一家店，合成一条，不要出现两次
- 标题卡是艺术字容易看错，**用同一时刻的口播字幕交叉验证**
  （例：标题卡看着像「呵根廷秘鲁餐厅」，字幕写「阿根廷秘鲁餐厅」，以字幕为准）
- **可爱字体/手写体常把汉字的偏旁拆得很开，笔画看着像拉丁字母——那还是个汉字，
  不是英文缩写。**遇到这种情况按整体字形猜最接近的汉字，不要把偏旁误读成
  「DZ」「NZ」这类字母组合直接抄进店名。真正的英文店名（如「Highline Bistro」）
  不受影响，正常照抄
- 菜名不是店名。「沙茶面」「辣子鸡」是某家店的招牌菜，不要单独列成一家店
- 店名不要自行改写或补全
- 只输出 JSON，不要任何其他文字"""


def claimed_count(desc):
    """从文案抠出宣称的店铺数量。用正则不用模型——模型知道这个数字就会凑数。"""
    m = re.search(r"(\d+)\s*家", desc)
    return int(m.group(1)) if m else None


def call_llm(sheets, desc="", city=""):
    base_url = os.environ["LLM_BASE_URL"].rstrip("/")
    # 文案提供菜系/地点线索，但把数量抹掉：模型一旦知道"7家"就会凑够 7 家
    ctx = (f"\n\n视频文案：{re.sub(r'[0-9]+ *家', '若干家', desc)}" if desc else "")
    ctx += f"\n城市：{city}" if city else ""
    body = {
        "model": os.environ["LLM_MODEL"],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT + ctx},
            *[{"type": "image_url",
               "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(s).decode()}}
              for s in sheets],
        ]}],
        "temperature": 0,
    }
    req = urllib.request.Request(
        f"{base_url}/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + os.environ["LLM_API_KEY"]})
    with urllib.request.urlopen(req, timeout=240) as r:
        resp = json.loads(r.read())
    u = resp.get("usage", {})
    print(f"· token: in={u.get('prompt_tokens', '?')} out={u.get('completion_tokens', '?')}",
          file=sys.stderr)
    text = resp["choices"][0]["message"]["content"]
    return json.loads(re.search(r"\{.*\}", text, re.S).group(0))


def recognize(meta, city=""):
    """识别店铺。数量和文案对不上就如实报出来，不重扫、不补齐。

    这里原本有个「数量不符就加密采样补扫」的机制，实测删掉了，两个原因：
      1. 1 秒采样的结果和 2 秒完全一致，补扫捞不到新东西，纯烧 token
      2. 博主标题夸大是常态（这条「7家」实际 6 家），补扫会变成常态浪费
    数量对不上多半是文案吹牛，不是我们漏了。把差异摆出来让人判断就够了。
    """
    desc = meta["desc"]
    claimed = claimed_count(desc)
    if meta["kind"] == "note":
        # 图文逐张调用，不是一次喂 4 张。实测一次喂完会漏：24 家的帖子只吐 18 家，
        # 输出越长模型越容易提前收尾。逐张的图片 token 总量相同，只多几份 prompt。
        print(f"· {len(meta['images'])} 张原图，逐张识别", file=sys.stderr)
        shops, seen = [], set()
        for i, u in enumerate(meta["images"], 1):
            # 单张图重试完还是不行，跳过它、别让一张图的网络抖动废掉已经识别出来
            # （且已经烧了 token）的其它几张。
            try:
                got = call_llm([compress(fetch(u))], desc, city).get("shops", [])
            except Exception as e:
                print(f"·   图 {i}: 跳过（{type(e).__name__}: {e}）", file=sys.stderr)
                continue
            fresh = [s for s in got if norm(s["name"], s.get("branch")) not in seen]
            seen.update(norm(s["name"], s.get("branch")) for s in got)
            print(f"·   图 {i}: {len(got)} 家，新增 {len(fresh)}", file=sys.stderr)
            shops.extend(fresh)
        for i, s in enumerate(shops, 1):  # 每张图的序号都从 1 开始，重排
            s["no"] = i
        return shops, claimed

    sheets = grab_sheets(meta["video_url"], FRAME_INTERVAL)
    print(f"· {len(sheets)} 张拼图 {sum(map(len, sheets)) // 1024} KB（不落盘）", file=sys.stderr)
    shops = call_llm(sheets, desc, city).get("shops", [])
    return sorted(shops, key=lambda s: s.get("no") or 0), claimed


def norm(name, branch=None):
    """去重键。只做保守清洗——「东京屋」和「东京屋静安店」是不是同一家，
    靠字符串答不了，那是坐标的活。宁可漏合并，不可错合并。

    branch 参与构键：博主特指「愚园路老店」时，它和没提分店的同名店不是一回事。
    """
    k = re.sub(r"[\s·・()（）]", "", name).lower()
    return f"{k}@{branch}" if branch and branch != "连锁不限" else k


def load_store():
    """把 shops.jsonl 聚合成店铺库：同一家店被多人推过就合并，次数是强信号。"""
    agg = {}
    if not STORE.exists():
        return agg
    for line in STORE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        k = norm(r["name"], r.get("branch"))
        e = agg.setdefault(k, {"name": r["name"], "branch": r.get("branch"),
                               "notes": [], "authors": [], "items": []})
        if r.get("note") and r["note"] not in e["notes"]:
            e["notes"].append(r["note"])
        if r["author"] not in e["authors"]:
            e["authors"].append(r["author"])
        if r["item_id"] not in e["items"]:
            e["items"].append(r["item_id"])
    return agg


def remove_shop(k):
    """按聚合键把一家店的所有记录行从 shops.jsonl 里删掉——服务器不留痕，
    不是本地隐藏。返回是否真删到了东西。"""
    if not STORE.exists():
        return False
    lines = [l for l in STORE.read_text(encoding="utf-8").splitlines() if l.strip()]
    keep = [l for l in lines if norm(json.loads(l)["name"], json.loads(l).get("branch")) != k]
    if len(keep) == len(lines):
        return False
    STORE.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    return True


def render_index(city="", fresh=(), meta=None, claimed=None):
    """全部店铺一页。已点过的自动沉底置灰——点评收藏夹读不到，
    只能记录你点没点过，这是唯一拿得到的信号。"""
    agg = load_store()
    # 构键必须和 load_store 一致：带 branch 的店，只用 name 算出来的键对不上
    fresh = {norm(s["name"], s.get("branch")) for s in fresh}

    def rank(kv):
        k, e = kv
        return (k not in fresh, -len(e["authors"]), e["authors"][0], e["name"])

    banner = ""
    if meta:
        diff = (f' · <b>文案称 {claimed} 家</b>'
                if claimed and claimed != len(fresh) else "")
        banner = (f'<div class=banner>刚导入 {meta["author"]} · '
                  f'{len(fresh)} 家{diff}</div>')

    rows = []
    for k, e in sorted(agg.items(), key=rank):
        tags = ('<i class=new>NEW</i>' if k in fresh else '') + \
               (f'<i class=hot>{len(e["authors"])} 人推过</i>' if len(e["authors"]) > 1 else '') + \
               (f'<i class=br>{e["branch"]}</i>' if e.get("branch") else '')
        rows.append(f"""
  <div class="item" data-k="{k}">
    <button type="button" class="del"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
         stroke-linecap="round" stroke-linejoin="round">
      <path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg></button>
    <a class="row" data-k="{k}"
       href="dianping://searchshoplist?keyword={e['name']}{'+' + e['branch'] if e.get('branch') and e['branch'] != '连锁不限' else ''}{'+' + city if city else ''}">
      <div class="txt"><b>{e['name']}{tags}</b><span>{' / '.join(e['notes'][:2])}</span>
        <em>{'、'.join(e['authors'])}</em></div>
      <div class="go">收藏 ›</div>
    </a>
  </div>""")

    html = f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title id=title>想吃 · {len(agg)} 家</title>
<style>
*{{box-sizing:border-box;margin:0}}
body{{font:16px/1.5 -apple-system,sans-serif;background:#f5f5f7;padding:16px;color:#111}}
h1{{font-size:17px}}
.sub{{color:#888;font-size:13px;margin-bottom:16px}}
.item{{position:relative;margin-bottom:8px}}
.del{{position:absolute;right:6px;top:6px;bottom:6px;width:56px;background:#ff453a;color:#fff;
     border:none;font:inherit;padding:0;cursor:pointer;-webkit-tap-highlight-color:transparent;
     -webkit-appearance:none;appearance:none;
     border-radius:10px;display:flex;align-items:center;justify-content:center;
     opacity:0;pointer-events:none;transition:opacity .15s}}
.del svg{{width:19px;height:19px}}
.del.show{{opacity:1;pointer-events:auto}}
.row{{display:flex;align-items:center;gap:12px;background:#fff;padding:14px 16px;
     border-radius:12px;text-decoration:none;color:inherit;position:relative;z-index:1;
     transition:transform .2s;touch-action:pan-y}}
.row:active{{background:#ececf0}}
.row.done{{opacity:.4}}
.row.done .go::after{{content:"已点过";color:#aaa}}
.row.done .go{{font-size:0}}
.txt{{flex:1;min-width:0}}
.txt b{{display:block;font-size:16px}}
.txt span{{color:#666;font-size:13px;display:block}}
.txt em{{color:#bbb;font-size:12px;font-style:normal}}
.go{{color:#ff6a00;font-size:14px;white-space:nowrap}}
i{{font-style:normal;font-size:11px;padding:1px 6px;border-radius:6px;margin-left:6px;
  vertical-align:1px}}
.new{{background:#ffe9e0;color:#e2500f}}
.hot{{background:#e8f2ff;color:#0b62d6}}
.br{{background:#eafaef;color:#0a7d3a}}
.banner{{background:#fff6e5;color:#8a5a00;font-size:13px;padding:10px 14px;
        border-radius:10px;margin-bottom:12px}}
</style>
<h1>想吃</h1>
<div class=sub id=sub>{len(agg)} 家 · 点过的会沉底 · 左滑删除</div>
{banner}
<div id=list>
{''.join(rows)}
</div>
<script>
const done = new Set(JSON.parse(localStorage.getItem('done') || '[]'))
const list = document.getElementById('list')

function resort() {{
  const items = [...list.children]
  items.sort((a, b) => {{
    const ad = a.querySelector('.row').classList.contains('done')
    const bd = b.querySelector('.row').classList.contains('done')
    return ad - bd
  }})
  items.forEach(it => list.appendChild(it))
}}

document.querySelectorAll('.item').forEach(item => {{
  const k = item.dataset.k
  const row = item.querySelector('.row')
  if (done.has(k)) row.classList.add('done')

  const del = item.querySelector('.del')
  let justDragged = false
  row.addEventListener('click', e => {{
    if (justDragged) {{
      justDragged = false
      e.preventDefault()
      return
    }}
    if (row._swiped) {{
      e.preventDefault()
      row.style.transform = ''
      del.classList.remove('show')
      row._swiped = false
      return
    }}
    done.add(k)
    localStorage.setItem('done', JSON.stringify([...done]))
    row.classList.add('done')
    resort()
  }})

  let x0 = 0, y0 = 0, dx = 0, dragging = false
  row.addEventListener('touchstart', e => {{
    x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; dx = 0; dragging = false
  }}, {{passive: true}})
  row.addEventListener('touchmove', e => {{
    const dxr = e.touches[0].clientX - x0, dyr = e.touches[0].clientY - y0
    if (!dragging && Math.abs(dxr) > Math.abs(dyr) && Math.abs(dxr) > 6) dragging = true
    if (!dragging) return
    dx = Math.max(-68, Math.min(0, dxr))
    row.style.transition = 'none'
    row.style.transform = `translateX(${{dx}}px)`
  }}, {{passive: true}})
  row.addEventListener('touchend', () => {{
    if (!dragging) return
    row.style.transition = ''
    const open = dx < -34
    row.style.transform = open ? 'translateX(-68px)' : ''
    del.classList.toggle('show', open)
    row._swiped = open
    justDragged = true
    dragging = false
  }})

  let delFired = false
  function doDelete() {{
    if (delFired) return
    delFired = true
    del.style.pointerEvents = 'none'
    fetch('remove', {{method: 'POST', body: JSON.stringify({{k}})}})
      .then(r => r.json())
      .then(res => {{
        if (!res.ok) throw new Error(res.msg || '删除失败')
        item.remove()
        updateCount()
      }})
      .catch(() => {{
        delFired = false
        del.style.pointerEvents = ''
        alert('删除失败，请重试')
      }})
  }}
  // 触屏上直接用 touchend 触发，不等浏览器补发的 click——
  // 那个合成 click 跟在前面的滑动手势后面，偶尔会被吞掉或延迟，
  // 表现就是"划开之后点一下没反应，得再点一次"。
  del.addEventListener('touchend', e => {{ e.preventDefault(); doDelete() }})
  del.addEventListener('click', doDelete)
}})

function updateCount() {{
  const n = list.children.length
  document.getElementById('sub').textContent = `${{n}} 家 · 点过的会沉底 · 左滑删除`
  document.getElementById('title').textContent = `想吃 · ${{n}} 家`
}}

resort()
updateCount()
</script>"""

    OUT.mkdir(exist_ok=True)
    path = OUT / "index.html"
    path.write_text(html, encoding="utf-8")
    return path, len(agg)


def process(raw, city=None):
    """跑完整条链路，返回摘要。CLI 和 HTTP 服务共用这一个入口。"""
    city = city if city is not None else os.environ.get("CITY", "")
    meta = parse_douyin(raw)
    kind = "图文" if meta["kind"] == "note" else f"视频 {meta['duration']}s"
    print(f"· {meta['author']} / {kind} / {meta['desc'][:30]}...", file=sys.stderr)

    shops, claimed = recognize(meta, city)
    print(f"· 识别到 {len(shops)} 家：{'、'.join(s['name'] for s in shops)}", file=sys.stderr)
    if claimed and len(shops) != claimed:
        print(f"· ⚠ 文案称 {claimed} 家，实际 {len(shops)} 家", file=sys.stderr)

    known = load_store()
    dup = [s["name"] for s in shops if norm(s["name"], s.get("branch")) in known]
    if dup:
        print(f"· 之前见过：{'、'.join(dup)}", file=sys.stderr)

    # 重跑同一条内容时先清掉旧记录，保证幂等
    if STORE.exists():
        keep = [l for l in STORE.read_text(encoding="utf-8").splitlines()
                if l.strip() and json.loads(l).get("item_id") != meta["item_id"]]
        STORE.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    with STORE.open("a", encoding="utf-8") as f:
        for s in shops:
            f.write(json.dumps({**s, "item_id": meta["item_id"],
                                "author": meta["author"]}, ensure_ascii=False) + "\n")

    path, total = render_index(city, shops, meta, claimed)
    print(f"· 库里共 {total} 家", file=sys.stderr)
    return {"author": meta["author"], "kind": meta["kind"], "count": len(shops),
            "claimed": claimed, "dup": dup, "total": total,
            "shops": [s["name"] for s in shops], "path": str(path)}


def main():
    raw = " ".join(sys.argv[1:]) or sys.stdin.read()
    print(process(raw)["path"])


if __name__ == "__main__":
    main()
