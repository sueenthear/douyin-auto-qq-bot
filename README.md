# douyin-auto-qq-bot

抖音分享链接 → QQ 自动解析机器人。在 QQ 里发一条抖音分享链接，机器人自动解析并回发：

| 类型 | 机器人行为 |
|------|-----------|
| **视频** | 引用回复「检测到抖音视频分享链接，正在解析中……」→ 发作品信息 → 发**无水印视频文件** |
| **图文** | 引用回复「检测到抖音图文分享链接，正在解析中……」→ 发作品信息 → 把图片构建为**合并转发**发送 |

遇到抖音风控（HTTP 403）时会**静默重新拉取 Cookie 并重试**，用户无感知。

> 仅供个人学习与研究，请遵守抖音平台条款与相关法律法规。

---

# 配置教程

## 0. 环境要求

| 依赖 | 说明 |
|------|------|
| Windows | 本项目在 Windows 上开发与实测 |
| Python 3.11+ | 需要 `requests` / `gmssl` / `selenium` / `websocket-client` |
| Edge 或 Chrome | 用于登录抖音、风控时重拉 Cookie |
| [NapCat](https://github.com/NapNeko/NapCatQQ) | QQ 协议端，提供 OneBot 11 接口 |
| 一个 QQ 小号（推荐） | 机器人登录用，避免主号被风控 |

安装 Python 依赖：

```powershell
pip install -r requirements.txt
```

---

## 1. 配置 NapCat

打开 NapCat 的 WebUI，新建一个 **WebSocket 服务器**（不是客户端），按下面填写：

| 配置项 | 值 | 说明 |
|--------|-----|------|
| 连接名称 | 任意，如 `ws-server` | |
| 监听 IP | `0.0.0.0` | 只本机用可填 `127.0.0.1` |
| 端口 | `3001` | 记下来，稍后填进 `config.json` |
| 监听路径 | `/` | |
| 连接角色 | **Universal（全双工，API + 事件）** | 必须全双工，否则只能收或只能发 |
| 心跳间隔 | `30000` | 单位 ms |
| 消息上报格式 | **Array（结构化数组）** | 本项目按数组格式解析 |
| 鉴权 Token | 自定，如 `123456` | 记下来，稍后填进 `config.json` |
| 强制事件推送 | ✅ 勾选 | |
| 上报 Bot 自身消息 | 可不勾选 | 勾了会收到自己发的消息 |

然后**启用此连接**，并确认 NapCat 已登录 QQ。

> 验证方法：直接进行第 4 步的 `--check`，能连上就说明 NapCat 配置正确。

---

## 2. 配置本项目

项目有**两个**配置文件，职责分离。

### 2.1 `config.json` —— 接入信息

```json
{
  "napcat": {
    "ws_url": "ws://127.0.0.1:3001/",
    "token": "123456",
    "reconnect_interval": 5,
    "read_timeout": 90
  },
  "download_dir": "downloads",
  "keep_files": false,
  "process_timeout": 300,
  "cookie_refresh_timeout": 60,
  "risk_retry_attempts": 3,
  "risk_retry_interval": 3,
  "request_min_interval": 1,
  "request_jitter_ratio": 0.5,
  "proxy": ""
}
```

| 字段 | 说明 |
|------|------|
| `napcat.ws_url` | 第 1 步填的 IP + 端口，格式 `ws://<IP>:<端口>/` |
| `napcat.token` | 第 1 步填的鉴权 Token；没设 Token 就留空字符串 |
| `napcat.reconnect_interval` | 断线重连间隔（秒） |
| `napcat.read_timeout` | 读超时（秒）。**必须大于 NapCat 心跳间隔**（默认 30s），否则空闲时会被误判断线；默认 90 足够 |
| `download_dir` | 媒体下载目录，仅在 `keep_files=true` 时使用 |
| `keep_files` | `false` = 下载到临时目录、发完即删（推荐）；`true` = 保留下载到 `download_dir` |
| `process_timeout` | 单个作品处理超时（秒） |
| `cookie_refresh_timeout` | 风控时等待新 Cookie 的上限（秒） |
| `risk_retry_attempts` | 风控时最多尝试解析的次数（含首次），默认 `3`；设为 `1` 表示不重试 |
| `risk_retry_interval` | 风控每次重试前的等待秒数，默认 `3` |
| `request_min_interval` | **请求节流**：两次抖音请求的最小间隔秒数，默认 `1`；频繁被风控就调大 |
| `request_jitter_ratio` | 节流的随机抖动比例，默认 `0.5`（实际间隔 = 1.0~1.5 倍） |
| `proxy` | 代理地址（如 `http://127.0.0.1:7890`），用于规避 IP 风控；空 = 直连 |
| `backend` | 协议端后端：`auto`（默认，连接后自动探测）/ `napcat` / `snowluma` |
| `messages` | （可选）自定义回复文案，见 2.3 |

### 2.2 `allow.txt` —— 监听白名单

**只有这里列出的会话**发来的抖音链接才会被处理，其他地方一律忽略。

```
// 注释：以 // 开头的整行会被忽略，空行也忽略
// # 开头 = 群号，* 开头 = 私聊号

*100000001
#200000003
```

| 写法 | 含义 |
|------|------|
| `*123456789` | 监听与 QQ `123456789` 的**私聊** |
| `#123456789` | 监听**群** `123456789` |

> 群号怎么查：QQ 里右键群 → 查看群信息。

### 2.3 `messages` —— 自定义文案（可选）

在 `config.json` 里加 `messages` 字段可覆盖默认文案，未覆盖的项保持默认：

```json
{
  "messages": {
    "video": "收到视频链接，正在解析中……",
    "image": "收到图文链接，正在解析中……",
    "unknown": "收到抖音链接，正在解析中……",
    "error": "解析失败了：{error}",
    "risk": "当前触发抖音风控（{reason}），已重试仍失败。链接：{url}"
  }
}
```

`{error}` / `{reason}` / `{url}` 是占位符，会被实际内容替换。

> `config.json` 和 `allow.txt` 都支持带 UTF-8 BOM 的文件（用记事本、PowerShell 编辑也不会出错）。

---

## 3. 登录抖音

首次使用需要登录一次，之后 Cookie 会自动保存复用。

```powershell
python main.py --login
```

会打开 Edge/Chrome 进入抖音，**扫码或账号密码登录**，程序自动抓取 Cookie 并保存到
`douyin_core/cookies.json`（已加入 `.gitignore`，不会被提交）。

检查登录状态：

```powershell
python main.py --check-only
```

登录已失效时会提示，重新跑 `--login` 即可。

---

## 4. 启动机器人

**推荐：双击 `start.bat`**，或：

```powershell
python launcher.py
```

`launcher.py` 会：

1. **启动前自检**：douyin_core 可用性 → 抖音登录态 → NapCat 连接
2. **启动机器人**并持续监控
3. 任一依赖挂掉（NapCat 断开 / 登录失效）→ **暂停并给出处理建议**，处理完按回车重新自检并启动

```
================================================================
  暂停：NapCat 连接失败（ws://127.0.0.1:3001/）：[WinError 10061] 无法连接。
        处理：确认 NapCat 已启动，且 config.json 里的 ws_url / token 正确
        处理完成后回车重新自检
================================================================
  处理完成后按回车继续（Ctrl+C 退出）
>
```

只自检不启动：

```powershell
python launcher.py --check
```

---

## 5. 验证是否成功

1. 用白名单里的 QQ 号，**私聊机器人**或在被监听的群里发一条抖音分享文案（App 里点分享 → 复制链接，整段粘贴即可）
2. 机器人应依次发出：引用回复 → 作品信息 → 视频文件 / 图文合并转发
3. 启动窗口的日志会实时打印过程：

```
[收到] private:100000001 ← https://v.douyin.com/xxxx/
[解析成功] 视频 7687183893074139874 魔女之夜 #CatQ…
[视频] 已下载 1.6 MB，开始上传…
```

遇到问题时先跑 `python launcher.py --check`，它会直接指出是哪一环没通。

---

# 常见问题

**机器人没反应？**
1. 确认发的号在 `allow.txt` 白名单里（群聊要写 `#群号`）
2. 确认 NapCat 在线（`python launcher.py --check`）
3. 确认消息里确实含抖音链接（要整段分享文案，或直接发 `https://v.douyin.com/xxx/`）

**解析失败？**
多为抖音风控或登录失效。程序会静默重拉 Cookie 并重试（默认最多 3 次、间隔 3s）；次数用尽仍失败会回发错误信息并附上触发链接。若频繁失败，跑 `python main.py --login` 重新登录，或调大 `config.json` 的 `risk_retry_attempts`。

**Bot 频繁断线重连？**
`config.json` 里 `napcat.read_timeout` 比 NapCat 的心跳间隔小。把它设到心跳的 2~3 倍（心跳 30s → 设 90）。

**视频发不出去 / 上传超时？**
视频文件较大时上传耗时较长，程序已给 180s 超时。若持续失败，检查 NapCat 所在机器的网络与磁盘。

**端口被占用？**
改 NapCat 的监听端口，同时改 `config.json` 的 `ws_url`，两者保持一致。

---

# 项目结构

```
douyin-auto-qq-bot/
├── start.bat                # 双击启动：激活 venv → 运行 launcher.py
├── launcher.py              # 一键自检 + 启动 + 监控（依赖挂掉时暂停等回车）
├── main.py                  # douyin_core 入口：自检 + 登录状态检测
├── config.json              # 接入信息（NapCat 地址 / token / 运行参数）
├── allow.txt                # 监听白名单：# 群号 / * 私聊号
├── requirements.txt
├── qq_bot/                  # QQ 机器人（NapCat / OneBot 11）
│   ├── config.py            # 加载 config.json + allow.txt
│   ├── napcat.py            # WS 客户端：echo 配对、自动重连、引用/转发/文件发送
│   ├── handler.py           # 核心流程：识别 → 引用回复 → info → 媒体 + 风控重试
│   ├── __main__.py          # 入口：python -m qq_bot
│   └── tests/               # 离线单元测试
└── douyin_core/             # 抖音解析内核（纯逻辑，无 UI）
    ├── douyin_parser.py     # 分享文案链接提取 / 重定向解析 / 信息组装
    ├── douyin_api.py        # 官方 Web API：浏览器参数 / msToken / ttwid / 签名 / 无水印地址
    ├── abogus.py            # a_bogus 签名（Apache-2.0，来自 F2）
    ├── xbogus.py            # X-Bogus 签名（Apache-2.0，回退用）
    ├── downloader.py        # 流式下载（进度回调、断点续传、多地址候选）
    ├── login_manager.py     # 登录态管理：检测 / 浏览器登录 / Cookie 抓取与刷新
    └── tests/               # 离线单元测试
```

## 命令行速查

```powershell
# 一键启动（自检 + 运行 + 监控）
python launcher.py
python launcher.py --check              # 只自检
python launcher.py --no-login-watch     # 关闭运行期登录态巡检

# 登录与自检
python main.py --login                  # 打开浏览器登录
python main.py --check-only             # 只检测登录状态

# 只解析不跑机器人
python -m douyin_core "<分享文案或链接>"
python -m douyin_core "<链接>" --json
python -m douyin_core "<链接>" --download ./out

# QQ bot（不含自检，需自行保证 NapCat 已就绪）
python -m qq_bot
python -m qq_bot --check
```

## 测试

```powershell
python -m pytest douyin_core/tests qq_bot/tests -q
```

全部为离线单测（网络请求均被 mock），无需联网、无需 NapCat。

---

# 实现要点（排障参考）

## 防风控措施

抖音对**请求频率**很敏感：连续解析几个作品就会返回
`403 Blocked by ArgusSecurityPlugin`。项目内置以下措施：

### 1. 请求节流（最有效）

所有对抖音的请求共享一个全局最小间隔，并叠加随机抖动，避免突发流量：

```json
{
  "request_min_interval": 1,
  "request_jitter_ratio": 0.5
}
```

- `request_min_interval`：两次请求之间的最小间隔（秒），默认 `1`
- `request_jitter_ratio`：额外随机抖动比例，默认 `0.5`（即实际间隔为
  `1.0 ~ 1.5` 秒随机），使请求节奏不像脚本

**觉得还是频繁被风控，就调大这两个值**（例如 `2` / `1.0`）。这是最直接的手段。

### 2. msToken 缓存

`default_query()` 原本每次调用都会打一次 `mssdk` 接口换取 msToken ——
即**每个作品解析都多一次请求**，请求量翻倍且更易触发风控。

现在生成结果会缓存 30 分钟（`_MS_TOKEN_TTL`），同一会话内只请求一次。

### 3. 代理支持

`config.json` 里配置代理可规避 **IP 维度**的风控：

```json
{
  "proxy": "http://127.0.0.1:7890"
}
```

留空表示直连。配置后，详情请求与 ttwid 注册都会走该代理。

### 4. 指数退避重试

单次请求遇 403/429 时，会在内部按 `1s → 2s → 5s` 退避重试（`_RETRY_DELAYS`），
再交给上层的「刷新 Cookie 重试」流程。

### 5. 风控后自动重登

见下一节。**注意顺序**：先靠节流「少触发」，触发后才走重登兜底 ——
频繁重登（每次都会开一次浏览器）本身也会加重风控。

### 实测基线

未开启节流时，连续解析 4 个作品，第 3 个即触发 403（风控率 25%）。
节流生效后请求被拉平，触发概率显著下降。

## 403 风控：静默刷新 Cookie 后重试

解析遇风控（HTTP 403/429、空响应、「需要登录」）时：

1. **不发任何报错**（「正在解析中……」回执照常发）
2. 启动浏览器复用 `.browser_profile` **重新拉取一次 Cookie**
   （profile 内已有登录态，通常无需重新扫码），同步给解析器
3. 等待 `risk_retry_interval` 后重试；**最多尝试 `risk_retry_attempts` 次**
4. 次数用尽仍失败，才发报错并附上触发链接

默认最多尝试 **3 次**、每次重试前等待 **3 秒**，可在 `config.json` 调整：

```json
{
  "risk_retry_attempts": 3,
  "risk_retry_interval": 3
}
```

日志形如：

```
[风控] 第 1/3 次触发（接口风控（HTTP 403）） —— 刷新 Cookie 后重试
[风控] 已重新拉取并保存 Cookie
[风控] 第 2/3 次触发（接口风控（HTTP 403）） —— 刷新 Cookie 后重试
[风控] 第 3/3 次尝试解析成功
```

若期间遇到非风控错误（如作品已删除），会立即返回该错误、不再重试。

实现上，`douyin_api.RiskControlError`（`DouyinAPIError` 子类）专门标识风控，
`douyin_parser._fetch_info` 会**显式放行**它而不是当作普通错误降级 —— 否则上层收不到
风控信号，自动重登逻辑形同虚设。

## 登录判据

抖音对未登录用户访问 `/user/self` **同样返回 HTTP 200** 的 SPA 页面，
只凭状态码判断会恒为「已登录」。

本项目改用 `/aweme/v1/web/user/profile/self/`，以响应体 `status_code` 为准：

| 响应 | 判定 |
|------|------|
| `status_code == 0` 或含 `user` | 已登录 |
| `status_code == 8` / `"用户未登录"` | 未登录（清空 Cookie 并引导重登） |
| 网络异常 / 非 JSON | 保守放行（避免网络抖动误清凭据） |

## 图文（图集）与视频

抖音图文作品（`aweme_type=68` / `media_type=2`）的内容本体在 `images` 数组里，
而其 `video.play_addr` **指向的是背景音乐音频**（`Content-Type: audio/mp4`）。
若只认 `video` 字段，会「成功」解析出一个 `.mp4` 后缀的音频文件，播放时纯黑无画面。

因此解析器优先识别 `images`：图片取 `url_list`（**无水印**，模板 `tplv-dy-aweme-images`），
`download_url_list`（带水印）仅作兜底；同图多副本时 jpeg/png 优先于 webp。

图文合并转发的节点结构为：**首个节点只放作品文案，其后每个节点只放一张图片**，
避免同一段文案在每张图上重复出现。

## NapCat 接入要点（实测）

- 连接需带 `Authorization: Bearer <token>` 头；请求用 `echo` 字段做请求-响应配对
- **本地文件写成 `file:///` + 正斜杠，并按 RFC 8089 做百分号编码**
  （等价于 Python `pathlib.Path(p).as_uri()`）
- 引用回复：message 数组首段 `{"type":"reply","data":{"id":"<message_id>"}}`
- 合并转发：`send_private_forward_msg` / `send_group_forward_msg`，
  节点 `{"type":"node","data":{"uin","name","content":[...]}}`，节点内可放图片
- 读超时必须大于 NapCat 心跳间隔，否则空闲时会被误判断线

## 协议端兼容：NapCat 与 SnowLuma

本项目同时适配 **NapCat** 与 **SnowLuma**（两者都是 OneBot v11 协议端，
NapCatQQ Desktop 可管理任一种）。两者对本地文件 URI 的**解析机制不同**，
这是「同一个机器人，换协议端后视频发不出去」的根因。

### 两者的解析实现（读源码实测）

| | 还原代码 | 机制 |
|---|---|---|
| NapCat | `decodeURIComponent(uri.slice(8))` <br>（`napcat.mjs`） | 切掉 `file:///` 前缀后直接解码，**不做 URL 解析** |
| SnowLuma | Node `fileURLToPath()` <br>（`index.mjs`） | 走 `new URL()`，**遵循 URI 语法** |

因此对未编码的特殊字符，两者行为**不一致**（实测矩阵）：

| 文件名含 | NapCat | SnowLuma |
|---|---|---|
| 普通字符 / 中文 / 空格 | ✅ | ✅ |
| `#` | ✅ | ❌ 被当 fragment **截断路径** → `ENOENT` |
| `?` | ✅ | ❌ 同上 |
| `%` | ❌ `URI malformed` | ❌ `URI malformed` |

### 解法：按 RFC 8089 编码

`to_file_uri()` 用 `pathlib.Path(p).as_uri()` 统一编码。这样做**对两者都安全**：

- **SnowLuma** 用 `fileURLToPath()` 解析，本就要求编码；
- **NapCat** 的 `decodeURIComponent` 会把编码**正确还原**，所以也能用。

注意：编码后 NapCat 侧的 URI 字符串与「不编码」写法**并不相同**
（中文路径会变成 `%E5%85%83` 形式），但因为 NapCat 会解码，**最终拿到的
本地路径完全一致**。旧实现「不做百分号编码」的前提是当时只跑 NapCat。

同时 `downloader.safe_filename()` 额外清洗 `#` 与 `%`：

1. 让文件名更干净可读；
2. 兜住**目录名**含这些字符的情况（例如下载目录本身带 `#`）。

> `?` 本就在 Windows 保留字符里，已被原有规则清洗。

### 后端选择：`backend`

```json
{ "backend": "auto" }
```

| 值 | 行为 |
|---|---|
| `auto`（默认） | 连接后调 `get_version_info`，按返回的 `app_name` 自动判定 |
| `napcat` | 强制按 NapCat 处理 |
| `snowluma` | 强制按 SnowLuma 处理 |

探测依据（实测）：SnowLuma 返回 `{"app_name": "SnowLuma", ...}`，
NapCat 返回含 `NapCat`。探测失败（旧版无该接口）时回退 `napcat`。

> 由于 URI 编码已统一，`backend` 目前**只影响日志展示**，两个后端都能正常工作。
> 该字段保留为显式声明与后续针对差异扩展的入口。

### 症状与根因（实测记录）

SnowLuma 下发视频时的日志：

```
send_private_msg failed: ENOENT: no such file or directory,
realpath 'C:\Users\...\Temp\douyin_bot_j3cqjrpc\眠眠羊毛衫_'
    at async stageSourceToDisk  (index.mjs:16572)
    at async loadVideo          (index.mjs:16908)
    at async uploadVideoMsgInfo (index.mjs:16953)
```

路径在 `眠眠羊毛衫_` 处**被截断**：作品标题是
`#厚黑 #ootd穿搭拍照 #厚黑美学`，拼出的文件名以 `#` 开头，
`file:///…` 里的 `#` 被 URI 解析器当作 fragment 起点，路径到此为止 ——
于是 `realpath` 找不到文件。

**复制该现象的等价最小验证**（Node）：

```js
fileURLToPath("file:///C:/t/眠眠羊毛衫_#厚黑_1.mp4")
// → "C:\\t\\眠眠羊毛衫_"     ← 被截断
fileURLToPath("file:///C:/t/%E7%9C%A0...%23%E5%8E%9A%E9%BB%91_1.mp4")
// → "C:\\t\\眠眠羊毛衫_#厚黑_1.mp4"   ← 正确还原
```

### 其他实测要点

- **视频段必须是消息里唯一的段**：SnowLuma 的 `assertVideoSendPolicy`
  要求一条消息只含一个 video 段且为唯一段，因此本项目单独发视频（不带文字）。
- **缩略图**：SnowLuma 读 OneBot 标准的 `data.thumb` 字段
  （`thumbUrl: data.thumb ? String(data.thumb) : undefined`），
  本项目发送视频时附上作品封面即被识别。SnowLuma 自带 `ffmpegAddon`
  （不需要系统装 ffmpeg），即使不给 `thumb` 也能自行抽帧。

---

# 来源与许可

签名算法 `douyin_core/abogus.py` / `douyin_core/xbogus.py` 来自开源项目
[jiji262/douyin-downloader](https://github.com/jiji262/douyin-downloader) 与
[F2](https://github.com/Johnserf-Seed/f2)（Apache-2.0），已保留原始版权声明。

仅用于个人学习与研究，请遵守抖音平台条款与相关法律法规。
