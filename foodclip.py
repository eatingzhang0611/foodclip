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
# 打点接口是 PC 站的，UA 得跟着报桌面，别用上面那个 iPhone 的
DESKTOP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/26.6.2 Safari/605.1.15")
_TTWID = None  # 进程内缓存，见 ttwid()
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
# 拼图密度。原来是 4x4@360，实测那是识别率低的主因之一：
# 单张图的视觉 token 被服务端钳在 1568 上限，所以 4x4 和 2x2 花的 token 一样多
# （实测 1440x2560 / 1080x2160 / 1520x1352 都是 ~1928 prompt token），
# 但 4x4 会把每帧压到 ~203px，烧录字幕直接糊成噪点。
# 密度换不来省钱，只换来看不清——所以退到 2x2，每帧 540 宽，字幕清晰可读。
TILE = "2x2"
FRAME_WIDTH = 540


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

    kind = "note" if kind == "note" else "video"
    # 分享页经常返回一个只有壳、没有 videoInfoRes 的降级版本——实测首次命中率只有
    # 四分之一左右，重试几次就好了。原来不重试，于是四次里三次直接报「识别失败」。
    page = None
    for attempt in range(6):
        time.sleep(random.uniform(0.4, 1.0) if attempt == 0 else random.uniform(1.5, 3.0))
        html = fetch(f"https://www.iesdouyin.com/share/{kind}/{item_id}/?region=CN&from_aid=1128",
                     {"Referer": "https://www.douyin.com/"}).decode("utf-8", "ignore")
        m2 = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\});?\s*</script>", html, re.S)
        if not m2:
            continue
        # loaderData 的 key 随内容类型变（video_(id)/page、note_(id)/page），别写死
        page = next((v for v in json.loads(m2.group(1))["loaderData"].values()
                     if isinstance(v, dict) and v.get("videoInfoRes", {}).get("item_list")), None)
        if page:
            break
    if not page:
        raise ValueError(f"分享页连续 6 次都没返回 videoInfoRes（item_id={item_id}）")
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


def ttwid():
    """跟字节要一个设备标识。这是拿打点接口唯一的门槛，一个进程注册一次就够。

    注意它跟登录毫无关系：register 接口谁都能调，返回的 ttwid 不绑任何账号。
    """
    global _TTWID
    if _TTWID is None:
        req = urllib.request.Request(
            "https://ttwid.bytedance.com/ttwid/union/register/",
            data=json.dumps({"region": "cn", "aid": 1768, "needFid": False,
                             "service": "www.ixigua.com", "union": True,
                             "cbUrlProtocol": "https",
                             "migrate_info": {"ticket": "", "source": "node"}}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": DESKTOP_UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            _TTWID = next(h.split(";")[0].split("=", 1)[1]
                          for h in r.headers.get_all("Set-Cookie") if h.startswith("ttwid="))
    return _TTWID


def fetch_chapters(item_id):
    """读视频进度条上的打点。博主自己手敲的店名，比让模型认画面里的艺术字准得多，
    而且不花 token。返回 [(秒, 店名), ...]，没打点的视频就是空列表。

    分享页那份 JSON 里 chapter_list 永远是空的，只有 www.douyin.com 的正式接口给内容。
    它看着门槛很高——浏览器发这个请求时挂着登录 cookie 和 a_bogus 签名——但实测两样
    都不需要：拆开单独试过，登录态摘干净照样 200，签名整个不带也照样 200，
    唯一承重的是 ttwid 这个设备标识，而它能直接向字节注册。所以这里不碰任何账号凭证。

    取不到就返回空，退回纯看图——打点是锦上添花，不是前置依赖。
    """
    url = ("https://www.douyin.com/aweme/v1/web/aweme/detail/?device_platform=webapp&aid=6383"
           "&channel=channel_pc_web&pc_client_type=1&version_code=190500&version_name=19.5.0"
           "&cookie_enabled=true&platform=PC&browser_language=zh-CN&browser_platform=MacIntel"
           f"&browser_name=Safari&browser_version=26.6.2&aweme_id={item_id}")
    # 403 是随机风控，不是凭证不对——跟分享页那个降级壳一个脾气，重试就好。
    for attempt in range(6):
        try:
            req = urllib.request.Request(url, headers={
                "Accept": "application/json, text/plain, */*", "Cookie": f"ttwid={ttwid()}",
                "User-Agent": DESKTOP_UA, "Accept-Encoding": "gzip",
                "Accept-Language": "zh-CN,zh-Hans;q=0.9",
                "Referer": f"https://www.douyin.com/video/{item_id}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            detail = json.loads(raw).get("aweme_detail") or {}
            return [(c["timestamp"] // 1000, c["desc"])
                    for c in (detail.get("chapter_list") or []) if c.get("desc")]
        except Exception as e:
            if attempt == 5:
                print(f"· 打点接口读不到（{type(e).__name__}: {e}），退回纯看图", file=sys.stderr)
            time.sleep(random.uniform(2, 4))
    return []


def grab_sheets(video_url, interval=None):
    """ffmpeg 直读远程 URL,抽帧拼图,JPEG 流走管道。视频一个字节都不落盘。"""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-user_agent", UA, "-headers", "Referer: https://www.douyin.com/\r\n",
        "-i", video_url,
        "-vf", f"fps=1/{interval or FRAME_INTERVAL},scale={FRAME_WIDTH}:-2,tile={TILE}",
        "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "-",
    ]
    blob = subprocess.run(cmd, capture_output=True, check=True).stdout
    # image2pipe 输出连续 JPEG,按 SOI/EOI 切开
    return [blob[i:j + 2] for i, j in
            zip([m.start() for m in re.finditer(b"\xff\xd8\xff", blob)],
                [m.start() for m in re.finditer(b"\xff\xd9", blob)])]


# 原来的 prompt 假设视频一定有「编号标题卡」，把字幕降级成交叉验证的辅助。
# 实测大量博主根本不用标题卡，店名只在口播字幕里，于是模型被引导去盯店招艺术字，
# 结果整条视频全靠猜——「闹闹食堂」「南波二郎拉面」这种字幕里白纸黑字的店名反而全漏。
# 现在改成先让模型判断这条内容用了哪几种载体，再按可靠度取名。
PROMPT = """这是一条美食推荐内容的画面，按时间顺序排列。

店名可能出现在下面任何一处，**每一处都要看，不要只盯着一处**：
1. **烧录字幕**（画面底部）—— 博主口播报店名，常见句式「第X家是…」「今天推荐…」
2. **店招/门头/菜单/小票/桌牌** —— 字幕不报名字时，名字只在这里
3. **编号标题卡**（如「01.店名」，常带描边）—— 有的博主用，没有也很正常
4. **大众点评/美团的店铺卡片截图** —— 带评分、评论数、人均

先判断这条内容用的是哪几种，再按对应的方式提取。**没有标题卡不代表没有店。**

计数线索：字幕里出现「第一家」「第二家」「第三家」…就是店铺分界线，
出现到第几家，就至少有几家店，一家都不能少报。

店名以哪个为准（按可靠度排序）：
- **博主自己打上去的字最可靠**——烧录字幕、图文上叠的配文、编号标题卡。
  这是他一个字一个字敲的，印刷体也不会看错 → 直接照抄
- 其次是点评/美团卡片标题
- 最后才是照片里实物上的字：店招艺术字、菜单、桌牌、产品卡、包装
  （既容易看错，也容易把产品名当店名，要拿旁边的配文交叉验证）
- **同一格/同一帧里这两者对不上时，一律以博主打的字为准。**
  不要拿照片里的字盖掉他写的店名，更不要当成两家店各存一条——
  一格配文只对应一家店，配文写的是哪家，那格就是哪家

输出 JSON 对象：
{"shops": [{"no": 序号, "name": "店名", "note": "一句话，25字内",
            "branch": 分店限定 或 null, "rec": "推荐" 或 "一般" 或 "踩雷"}]}

点评卡片标题要拆开：括号外的主名放 name，括号里的分店放 branch
（例：「La Mia Casa漫味意式小馆（上海路店）」→ name="La Mia Casa漫味意式小馆"，
  branch="上海路店"。**name 里不要再带括号，会污染搜索关键词**）

note 写什么：
- 有点评卡片时，**优先抄客观数据**：「4.8分 ¥122/人 意大利菜」
- 没有客观数据时，写字幕里的招牌菜/特色：「招牌黄鱼烩面，焖肉面一般」
- 不要写「吃意大利菜来这」这种照搬标语的废话，那是标题不是信息

rec 字段（博主对这家店的态度，只看他说出口的话，不要替他品评）：
- "推荐" —— **默认值**。美食推荐内容里绝大多数店博主都是真心在推，拿不准就填这个
- "一般" —— 博主明显有保留：只是顺带一提、夸的是环境/氛围而不是吃的、
  或者说了「没有很惊艳」「不如前面那家」「就那样」「人多但也就一般」这类话
- "踩雷" —— 明说不好吃、不会再来、提醒别人避雷
没有明确保留的一律是"推荐"。语气热不热闹不算依据，博主说了什么才算。

branch 字段规则（这条最容易做错，看清楚）：
- 字幕里只要出现**连锁、分店、就近、哪一家、总店、老店、某某路/某某广场店**这类说法，
  就要填。照抄原话的说法，别自己概括：
    「一定要去愚园路那家老店」   → "愚园路老店"
    「这家店就是个连锁店了，大家可以选自己就近的」 → "连锁不限"
    「他家有好几家，我推荐静安寺那家」 → "静安寺店"
- **完全没提到分店这回事，才填 null。不要根据店名猜，不要补全。**
  「东京屋」你无法从名字知道它有没有分店——那就是 null，不是你该判断的事

找不到名字时怎么办（**最重要的一条**）：
- 字幕说了「第X家是思南路上阿娘面馆原班人马开的」但全程没报店名、店招也拍不清 →
  name 填字幕里的**原话描述**（如「思南路 阿娘面馆原班人马」），
  note 里注明「画面未出现店名」。
- **绝对不要编一个看起来像店名的词。** 拿不准的字宁可留描述，也不要猜。
  编出来的名字会直接导致搜错店，比漏掉更糟。

其他严格要求：
- 文案里提到但画面里没有的店，也收——文案是作者亲手打的，可信度高于画面推断
- **同一家店只输出一条。**同一家店在不同画面反复出现是常态，不要每出现一次就写一条
- **产品名不是店名。**菜名（「沙茶面」「辣子鸡」）、菜单和小票上的单品、
  商品包装/吊牌/产品卡上印的品名（豆种、酒标、伴手礼盒名），都是店里卖的东西，
  不要单列成一家店。判据：它是**一样能点能买的东西**，还是**一个能走进去的地方**？
  分不清就不要收。一个好使的判据：牌子上如果还印着产地/庄园/海拔/品种/处理法/
  风味/年份/度数/克数/价格这类参数，那是产品说明卡，不是店招——这类卡片常常
  设计得比店招还讲究、名字写得比店名还大，但它不是店
- **可爱字体/手写体常把汉字的偏旁拆得很开，笔画看着像拉丁字母——那还是个汉字，
  不是英文缩写。**按整体字形猜最接近的汉字，不要把偏旁误读成「DZ」「NZ」这类
  字母组合抄进店名。真正的英文店名（如「Highline Bistro」）正常照抄
- 店名不要自行改写或补全
- 只输出 JSON，不要任何其他文字"""


def claimed_count(desc):
    """从文案抠出宣称的店铺数量。用正则不用模型——模型知道这个数字就会凑数。"""
    m = re.search(r"(\d+)\s*家", desc)
    return int(m.group(1)) if m else None


def call_llm(sheets, desc="", city="", chapters=()):
    base_url = os.environ["LLM_BASE_URL"].rstrip("/")
    # 文案提供菜系/地点线索，但把数量抹掉：模型一旦知道"7家"就会凑够 7 家
    ctx = (f"\n\n视频文案：{re.sub(r'[0-9]+ *家', '若干家', desc)}" if desc else "")
    ctx += f"\n城市：{city}" if city else ""
    if chapters:
        # 打点是博主手敲的，写法比画面里的店招艺术字可靠，所以名字以它为准。
        # 但不能当白名单：实测打点常常只标一半，画面里有、打点里没有的店照样得收。
        ctx += ("\n\n视频进度条打点（博主自己标的。店名写法以这个为准；"
                "但打点常常不全，画面里出现了而打点里没有的店，照样要收进来。"
                "「引言」「开头」「结尾」「彩蛋」这类不是店名，跳过）：\n"
                + "\n".join(f"{t // 60:02d}:{t % 60:02d} {d}" for t, d in chapters))
    # 不传 temperature：claude-sonnet-5 / opus-5 直接 400
    # （"`temperature` is deprecated for this model"），haiku 才收。
    # max_tokens 必须显式给：一条 20+ 家的攻略视频输出能到 7k+ token，
    # 走默认上限会被截断成半个 JSON，解析直接炸。
    body = {
        "model": os.environ["LLM_MODEL"],
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT + ctx},
            *[{"type": "image_url",
               "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(s).decode()}}
              for s in sheets],
        ]}],
    }
    req = urllib.request.Request(
        f"{base_url}/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + os.environ["LLM_API_KEY"]})
    with urllib.request.urlopen(req, timeout=900) as r:
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
        shops = []
        for i, u in enumerate(meta["images"], 1):
            # 单张图重试完还是不行，跳过它、别让一张图的网络抖动废掉已经识别出来
            # （且已经烧了 token）的其它几张。
            try:
                got = call_llm([compress(fetch(u))], desc, city).get("shops", [])
            except Exception as e:
                print(f"·   图 {i}: 跳过（{type(e).__name__}: {e}）", file=sys.stderr)
                continue
            # 整体重去一遍而不是只筛新的：后一张图给出的名字可能比前一张更全
            # （带上了分店），dedup 会拿它替换掉先前那条残缺的。
            before = len(shops)
            shops = dedup(shops + got)
            print(f"·   图 {i}: {len(got)} 家，新增 {len(shops) - before}", file=sys.stderr)
        for i, s in enumerate(shops, 1):  # 每张图的序号都从 1 开始，重排
            s["no"] = i
        return shops, claimed

    chapters = fetch_chapters(meta["item_id"])
    if chapters:
        print(f"· 进度条打点 {len(chapters)} 个：{'、'.join(d for _, d in chapters)}", file=sys.stderr)
    sheets = grab_sheets(meta["video_url"], FRAME_INTERVAL)
    print(f"· {len(sheets)} 张拼图 {sum(map(len, sheets)) // 1024} KB（不落盘）", file=sys.stderr)
    shops = call_llm(sheets, desc, city, chapters).get("shops", [])
    # 视频是一次调用，本来以为不会自己重复，实测会：模型偶尔陷进复读循环，
    # 同一家店连吐十几条一路写进 shops.jsonl。这里自己去一次重。
    return dedup(sorted(shops, key=lambda s: s.get("no") or 0)), claimed


def norm(name, branch=None):
    """去重键。只做保守清洗——「东京屋」和「东京屋静安店」是不是同一家，
    靠字符串答不了，那是坐标的活。宁可漏合并，不可错合并。

    branch 参与构键：博主特指「愚园路老店」时，它和没提分店的同名店不是一回事。
    但 branch 要拼进名字里、不能当独立字段拼——同一家店在不同图上，模型有时拆成
    name="Gregorius SHADA" + branch="安福"，有时整个塞进 name="Gregorius SHADA安福"。
    拆法不一样不该算两家店，拼起来归一化就都是同一个键了。
    """
    return re.sub(r"[\s·・()（）]", "",
                  name + (branch if branch and branch != "连锁不限" else "")).lower()


def same_shop(a, b):
    """两个归一化键算不算同一家店。**只在同一条内容内部用**，理由见下。

    norm() 把 name+branch 拼起来只解决了"拆法不同"，解决不了"有没有拼"：
    模型给同一家店，这张图吐 "Gregorius SHADA"、下张图吐 "Gregorius SHADA安福"，
    两个键一长一短谁也不等于谁，于是同一家店存成两条。所以再认一种情况——
    一个键是另一个的前缀，多出来的那截很短，就当同一家。

    但这个判据本身是不可靠的：「面屋武藏」和「面屋武藏虎穴」也满足它，那是两家店。
    长度阈值救不了——"虎穴"和"安福"都是两个字，字符串层面分不出哪个是分店后缀。
    所以它只在一条内容内部成立：同一个博主在同一条视频/图文里，前缀相同的两个名字
    几乎必然是他自己写法不统一。跨内容就不能这么并了，load_store 仍然按精确键聚合——
    宁可列表里多一行，也不能把两家店合成一家。
    """
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    return bool(short) and long.startswith(short) and len(long) - len(short) <= 5


def dedup(shops):
    """同一家店只留一条，保留名字更全的那条（更长通常意味着带着分店后缀）。
    保持传入顺序，排序是调用方的事。"""
    out = []
    for s in shops:
        k = norm(s.get("name", ""), s.get("branch"))
        hit = next((i for i, o in enumerate(out)
                    if same_shop(k, norm(o["name"], o.get("branch")))), None)
        if hit is None:
            out.append(s)
        elif len(k) > len(norm(out[hit]["name"], out[hit].get("branch"))):
            out[hit] = s
    return out


# 推荐度排序权重。缺字段按"推荐"算——rec 是后加的，老记录一条都没有，
# 不能让它们凭空沉底。
REC_SEV = {"推荐": 0, "一般": 1, "踩雷": 2}


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
                               "notes": [], "authors": [], "items": [], "rec": None})
        # 多人推过时取最正面的那个态度：一个人觉得一般，不该盖过另一个人的力荐。
        # 模型偶尔会自创一个词（「力荐」之类），不在表里的一律当"推荐"
        rec = r["rec"] if r.get("rec") in REC_SEV else "推荐"
        if e["rec"] is None or REC_SEV[rec] < REC_SEV[e["rec"]]:
            e["rec"] = rec
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
        # 博主没真心推的往下沉，但排在"刚导入"之后——刚导入的那批要整批可见，
        # 包括其中被标"一般"的，不然用户看不到这次识别都收了些什么。
        return (k not in fresh, REC_SEV[e["rec"]], -len(e["authors"]),
                e["authors"][0], e["name"])

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
               (f'<i class=br>{e["branch"]}</i>' if e.get("branch") else '') + \
               ('' if e["rec"] == "推荐" else f'<i class=meh>{e["rec"]}</i>')
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
.meh{{background:#eee;color:#888}}
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
