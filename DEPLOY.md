# 部署到 Ubuntu

整个服务只依赖 `python3` 和 `ffmpeg`，没有 pip 包、没有虚拟环境、没有数据库。

## 1. 装依赖

```bash
sudo apt update && sudo apt install -y python3 ffmpeg
```

## 2. 传代码

```bash
scp -r ~/Desktop/foodclip you@你的服务器:~/foodclip
```

`.env` 里的 `LLM_API_KEY` 会一起传过去，确认权限：

```bash
chmod 600 ~/foodclip/.env
```

## 3. 单跑一条，确认服务器上通

```bash
cd ~/foodclip && python3 foodclip.py "https://v.douyin.com/MiUx_mVUFgc/"
```

看到 `识别到 6 家：...` 就说明 ffmpeg 和 API key 都没问题。

## 4. 常驻服务

```bash
sudo tee /etc/systemd/system/foodclip.service > /dev/null <<'EOF'
[Unit]
Description=foodclip
After=network.target

[Service]
Type=simple
User=%i
WorkingDirectory=/home/你的用户名/foodclip
ExecStart=/usr/bin/python3 /home/你的用户名/foodclip/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo sed -i "s/%i/$USER/;s|/home/你的用户名|$HOME|g" /etc/systemd/system/foodclip.service
sudo systemctl daemon-reload
sudo systemctl enable --now foodclip
```

首次启动会在 `.env` 里生成一个 `SECRET`，拿出来：

```bash
grep SECRET ~/foodclip/.env
journalctl -u foodclip -n 5
```

服务只监听 `127.0.0.1:8788`，不直接对公网暴露。

## 5. Caddy

```
foodclip.你的域名 {
    reverse_proxy 127.0.0.1:8788
}
```

```bash
sudo systemctl reload caddy
```

HTTPS 证书 Caddy 自动申请。验证：

```bash
curl https://foodclip.你的域名/health     # 应返回 ok
```

## 6. 你的两个 URL

把 `<SECRET>` 换成第 4 步拿到的值：

| 用途 | URL |
|---|---|
| 快捷指令 POST | `https://foodclip.你的域名/<SECRET>/add` |
| 手机上看清单 | `https://foodclip.你的域名/<SECRET>/` |

**第二个存成 Safari 书签，加到主屏幕。** 固定 URL 意味着「点过的沉底」记录永久保留。

安全模型是「不可猜的路径即凭证」：`SECRET` 是 24 字节随机串，不泄露就没人能访问。别把带 SECRET 的链接贴到公开地方。

---

# iOS 快捷指令

## 建快捷指令

1. 打开「快捷指令」App → 右上角 `+`
2. 点顶部标题 → 「重命名」→ 叫「存到想吃」
3. 点标题 → 「详细信息」→ 勾选 **「在共享表单中显示」**
4. 「共享表单类型」只留 **文本** 和 **URL**
5. 加动作 **「获取 URL 内容」**：
   - URL 填 `https://foodclip.你的域名/<SECRET>/add`
   - 方法改成 **POST**
   - 请求体选 **文件**，值选 **「快捷指令输入」**
6. 加动作 **「显示通知」**，内容选上一步结果里的 `msg`
7. 加动作 **「打开 URL」**，填 `https://foodclip.你的域名/<SECRET>/`

## 用法

抖音里点分享 → 「更多」→ 「存到想吃」 → 等 20~40 秒 → 通知弹出「you千花 · 6 家」→ 自动跳到清单页。

> 一条要跑 20~40 秒，快捷指令会一直转圈。分享完可以直接锁屏，跑完会推通知。

---

# 日常运维

```bash
journalctl -u foodclip -f          # 看日志
systemctl restart foodclip         # 重启
wc -l ~/foodclip/shops.jsonl       # 攒了多少家
```

磁盘占用：代码 32 KB + 每家店约 150 字节。**一年攒一千家也就 150 KB。** 视频和帧图全程走管道，不落盘。
