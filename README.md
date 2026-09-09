# foodclip

刷到一条美食视频，分享给它，几分钟后手机上多出一份可点开的店铺清单——每家店直接跳大众点评。

只依赖 `python3` 和 `ffmpeg`。没有 pip 包、没有数据库、没有前端框架，全部数据就是一个 `shops.jsonl`。

## 它做了什么

抖音链接进去，一份清单出来：

1. **拿内容** —— 解析短链，抓视频/图文和文案。视频用 ffmpeg 直读远程 URL，按 2 秒抽帧拼成大图；图文直接取原图。**全程走管道，视频不落盘。**
2. **认店** —— 拼图 + 文案 + 进度条打点一起丢给视觉模型，让它读字幕、标题卡、店招、点评卡片，吐出店名 / 一句话点评 / 分店 / 推荐度。
3. **出清单** —— 按店名归并（多人推过的排前面），渲染成一页 `index.html`，每家店一个 `dianping://` 深链。

## 用起来长这样

抖音里点分享 →「存到想吃」快捷指令 → 锁屏等一会 → ntfy 弹通知「xxx · 6 家」→ 点开就是清单页。

清单页上：多人推过的置顶，博主没真心推的（自动标「一般」「踩雷」）沉底，点过的自动置灰。

## 本地跑一条

```bash
brew install ffmpeg          # 或 apt install ffmpeg
cp .env.example .env         # 填 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
python3 foodclip.py "7.94 复制打开抖音... https://v.douyin.com/xxx/ ..."
```

模型必须支持图片输入。`.env.example` 里列了几家现成的（豆包、通义、GLM、Kimi、Claude）。

跑完看 `out/index.html`。

## 部署成常驻服务

配 systemd + Caddy + iOS 快捷指令，见 **[DEPLOY.md](DEPLOY.md)**。

## 文件

| | |
|---|---|
| `foodclip.py` | 全部逻辑：抓取、抽帧、识别、渲染 |
| `server.py` | HTTP 入口，给快捷指令用。鉴权靠不可猜的路径 |
| `shops.jsonl` | 唯一的持久化数据，一家店一行 |
| `out/index.html` | 渲染出来的清单页 |

一年攒一千家也就 150 KB。
