<div align="center">

# 🎙 EchoNote · 声记

**本地运行的开源录音 + 实时语音转文字工具**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Whisper](https://img.shields.io/badge/faster--whisper-local%20STT-412991?style=flat-square&logo=openai&logoColor=white)](https://github.com/SYSTRAN/faster-whisper)
[![License](https://img.shields.io/badge/License-MIT-64ffda?style=flat-square)](LICENSE)
[![Stage](https://img.shields.io/badge/stage-v0.9.0%20tech%20preview-f0b429?style=flat-square)](#-延迟口径重要)

边说边出字 · 录音不上传 · 安装后可离线 · 支持中英日韩等多语言

</div>

---

> **这是 v0.9.0 技术预览（tech preview），不是 v1.0.0。**
> 三件事需要先说清楚：**实验性**；**延迟取决于硬件**（见下文的口径，尚未达标）；**首次运行需要联网下载模型**，之后可以离线使用。

---

## ✨ 功能

- 🎙 **一键录音** —— 浏览器麦克风采集，16 kHz 单声道 PCM
- ⚡ **实时转写** —— 边说边出字，停顿后自动断句
- 🔒 **完全本地** —— 语音识别使用本地 faster-whisper 模型，音频不上传任何服务器
- 🌍 **多语言** —— 中文 / 英文 / 日文 / 韩文 / 法文 / 德文 / 西班牙文 / 俄文，可自动识别
- 📝 **四种导出** —— TXT 文本、Markdown 笔记、SRT 字幕、WAV 原始录音
- 🎨 **深色界面** —— 录音状态、实时波形、计时器、自动滚动字幕
- ⌨️ **全键盘可用** —— 空格开始 / 停止，导出菜单支持方向键与 Esc
- ♿ **读屏友好** —— 状态与字幕通过 live region 播报，错误使用 `role="alert"`

![EchoNote 界面](docs/screenshot.png)

> 截图取自 v0.9.0 实际运行（真实模型 + 8.95 s 中文样例音频）。全套流程见
> `docs/screenshots/`：空态 → 录音中 → 停止后字幕 → 导出菜单 → 窄屏。
> 该次录音导出的四种文件在 `docs/examples/`。

---

## 📥 下载即用（Windows，不用装 Python）

去 [Releases](https://github.com/Qsianuo/echonote/releases) 下载最新的
`EchoNote-*-windows-x64.zip`，解压后双击 `EchoNote.exe` 即可。

1. 解压到**任意可写目录**（例如 `D:\EchoNote`），不要放 `C:\Program Files`
2. 双击 `EchoNote.exe`，会弹出一个黑色命令行窗口
3. 模型加载完成后浏览器自动打开，点麦克风开始录音
4. 关闭命令行窗口即退出

**首次启动要下载约 464 MB 的识别模型**（只下载一次，之后离线可用）。国内网络较慢时：

```bat
set HF_ENDPOINT=https://hf-mirror.com
EchoNote.exe
```

排查问题用自检命令，会把原生依赖和前端资源逐项列出来：

```bat
EchoNote.exe --self-test
```

> 绿色版，不写注册表、不留后台服务。录音与识别全在本机，音频不会离开电脑。

---

## 🚀 从源码运行

### Windows（推荐）

1. 安装 [Python 3.10+](https://www.python.org/downloads/)（安装时勾选 **Add Python to PATH**）
2. 双击 **`start.bat`**
3. 脚本会先等模型就绪，再自动打开 `http://127.0.0.1:8000`
4. 顶部显示「模型就绪」后，点击麦克风开始录音

启动脚本只在 `requirements.txt` 变化时安装依赖，因此**离线二次启动不会被 pip 阻塞**。

### macOS / Linux

```bash
chmod +x run.sh
./run.sh
```

### 手动启动

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m app
```

> 用 `python -m app` 而不是直接 `uvicorn`，可以保留「非本机地址默认拒绝启动」的保护。

### 自己打包

```bash
pip install pyinstaller
pyinstaller packaging/echonote.spec --noconfirm
python packaging/smoke_test.py dist/EchoNote/EchoNote.exe
```

产物在 `dist/EchoNote/`，整个目录压缩后即可分发。推送 `v*` 标签会由
`.github/workflows/release.yml` 自动构建并附加到 Release。

---

## ⚡ 延迟口径（重要）

延迟强依赖 CPU 与模型，**不要用单一数字概括**。EchoNote 采用分档判定：

| 档位 | 硬件 | 首条 partial（相对语音起点） | final（相对句末） |
|------|------|------------------------------|-------------------|
| **A** | ≥8 逻辑核 + `small` + int8 | 目标 p95 ≤ 2.0 s | 目标 p95 ≤ 3.0 s |
| B | 4 核 | 披露，不判定 | 披露，不判定 |
| C | ≥12 核 | 目标 1.5 s | 目标 2.5 s |

- 判定要求 **≥30 句样本 × 3 次运行取中位**，不是两台机器上的两支观测值。
- 开发机（8 核 / `small`）当前实测 final 为 **4.24–4.90 s（n=2）**，**尚未达标**；这也是它仍是技术预览的原因之一。
- `ECHONOTE_PARTIAL_INTERVAL_S` 只是**下限节流**，不是对外承诺值。

---

## 🌐 国内网络提示

模型托管在 Hugging Face，首次下载可能较慢。可使用镜像：

```bash
# Windows CMD
set HF_ENDPOINT=https://hf-mirror.com

# PowerShell
$env:HF_ENDPOINT="https://hf-mirror.com"

# macOS / Linux
export HF_ENDPOINT=https://hf-mirror.com
```

也可以先把模型放到 `models/faster-whisper-small/`，再用 `ECHONOTE_MODEL` 指向它，实现完全离线启动。

---

## ⚙️ 配置

所有配置通过环境变量覆盖，无需改代码：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ECHONOTE_MODEL` | `small` | 模型：`tiny` / `base` / `small` / `medium` / `large-v3`，或本地目录 |
| `ECHONOTE_MODEL_DIR` | `./models` | 模型缓存目录 |
| `ECHONOTE_LANGUAGE` | `zh` | 默认语言，如 `zh`、`en`；`auto` 为自动识别 |
| `ECHONOTE_VAD_THRESHOLD` | `0.010` | 能量阈值。**数值越大越不灵敏**（更难触发），越小越容易把噪声当语音 |
| `ECHONOTE_VAD_HANGOVER_MS` | `650` | 停顿多久算一句结束。按 30 ms 向下取整，**实际 630 ms** |
| `ECHONOTE_VAD_MIN_SPEECH_MS` | `280` | 最短语音，按帧取整后**实际 270 ms**，更短的片段直接丢弃 |
| `ECHONOTE_VAD_MAX_SEGMENT_S` | `15` | 单段最长时长，到点强制切分 |
| `ECHONOTE_PARTIAL_INTERVAL_S` | `1.2` | 实时字幕**下限节流**间隔，不是承诺值 |
| `ECHONOTE_MAX_RECORD_MINUTES` | `90` | 录音时长软上限；`0` 表示不限（界面会提示内存风险自负） |
| `ECHONOTE_HOST` | `127.0.0.1` | 监听地址（启动脚本会让它生效） |
| `ECHONOTE_PORT` | `8000` | 端口（启动脚本会让它生效） |
| `ECHONOTE_ALLOW_NONLOCAL` | 未设置 | 允许非回环监听。必须同时设置令牌，否则拒绝启动 |
| `ECHONOTE_ACCESS_TOKEN` | 空 | 非回环部署时的访问令牌，WebSocket 需带 `?token=` |
| `ECHONOTE_ALLOWED_ORIGINS` | 空 | 额外允许的 Origin，逗号分隔 |

### 模型选择建议

| 模型 | 体积 | 速度 | 中文准确率 | 适合 |
|------|------|------|-----------|------|
| `tiny` | ~75 MB | 最快 | 一般 | 低配试水 |
| `base` | ~145 MB | 快 | 可用 | 普通配置 |
| **`small`** | ~490 MB | 适中 | **好** | **默认推荐** |
| `medium` | ~1.5 GB | 慢 | 很好 | 高配 / 高精度 |

> 纯 CPU 即可运行（int8 量化）。独立显卡不是必需的。

---

## 🧱 使用边界

| 项 | 值 |
|----|----|
| 上行音频 | Int16 PCM / 16 kHz / 单声道，**合并后每帧 ≥100 ms**，服务端约 10 帧/秒 |
| 单帧上限 | 32 KiB，超出即断开并返回 `frame_too_large` |
| 服务端积压 | 最多缓存 60 s 音频，溢出时**丢最旧、保最新**并回 `warning`（不静默） |
| 并发会话 | 单进程最多 2 个，第 3 个收到 `busy` |
| 录音时长 | 默认软上限 90 分钟，72 分钟提示；到上限自动停止但保留全部文字与音频 |
| 落盘 | **不写磁盘**（模型文件除外）。关闭或刷新页面即丢失，导出由你手动触发 |

---

## 🔧 工作原理

```text
浏览器麦克风
   │  getMediaStream + AudioWorklet
   ▼
16 kHz 单声道 PCM（worklet 每 8 ms 一块）
   │  合并为 ≥100 ms 帧
   ▼
WebSocket ──▶ FastAPI（每连接一会话）
                 │
                 ▼
           能量 VAD 自动断句
            │              │
     （说到一半）      （停顿 ≥630 ms）
            │              │
            ▼              ▼
        faster-whisper 转写
            │              │
            ▼              ▼
       partial 字幕     final 字幕（带 epoch + 时间戳）
            └──────┬───────┘
                   ▼
             浏览器实时显示
```

- **分段识别**：按停顿切分语音，避免整段长音频等待
- **部分结果**：说话过程中按配置节流刷新，可被后续结果覆盖
- **最终结果**：停顿后生成确定文本，带会话内时间戳
- **时间轴**：每个会话携带 `epoch` 与 `origin_ms`，断线重连后绝对时间仍然连续
- **本地推理**：`faster-whisper` + CTranslate2 int8，CPU 友好

---

## 📤 导出格式

| 格式 | 内容 |
|------|------|
| **WAV** | 16 kHz / 16-bit / 单声道原始录音 |
| **TXT** | 头信息（录音时间、结束时间、时长、模型、语言）+ `[mm:ss] 文本` |
| **Markdown** | 同元数据 + 列表形式，适合粘贴到笔记 / 博客 |
| **SRT** | 标准字幕，时间轴为**录音绝对时间**，断线处保留空洞 |

> 「录音时间」是**开始录音的时刻**，不是点导出的时刻。23:59 开始、00:01 导出，文件里的日期仍然是前一天。

---

## 🔒 隐私

- 音频只在本机的 `127.0.0.1` 与服务端通信，**不经过互联网**
- 识别使用本地模型，**不需要 API Key**
- 录音与转写**不写入磁盘**，也不使用 OPFS / IndexedDB / 临时文件（导出的文件由你自己保存）
- 无遥测、无统计、无第三方请求（仅首次模型下载）
- 日志不记录转写文本

---

## 🧩 项目结构

```text
echonote/
├── app/
│   ├── __main__.py    # python -m app（含监听地址安全检查）
│   ├── desktop.py     # 便携版入口：横幅、自动开浏览器、--self-test
│   ├── main.py        # FastAPI、WebSocket 会话、协议 v2、并发与队列上限
│   ├── protocol.py    # 协议 v2 消息构造与校验
│   ├── stt.py         # faster-whisper 加载（可重试）与转写
│   ├── vad.py         # 能量 VAD 自动断句
│   └── config.py      # 环境变量配置（含 frozen 路径解析）
├── web/
│   ├── index.html     # 界面
│   ├── style.css      # 深色主题
│   ├── app.js         # 录音、时间轴、实时字幕、导出编排
│   ├── export.js      # TXT/MD/SRT 生成（纯函数，可测试）
│   ├── wav.js         # WAV 编码（纯函数，可测试）
│   └── pcm-worklet.js # 音频重采样 Worklet
├── packaging/
│   ├── echonote.spec  # PyInstaller 配置（收集原生 DLL 与 VAD 模型）
│   ├── launch.py      # 打包入口
│   ├── smoke_test.py  # 冻结产物冒烟测试
│   └── build.ps1      # 本地与 CI 共用的构建脚本
├── tools/
│   ├── preflight.py   # 启动前检查：模型缓存 / 网络
│   └── wait_ready.py  # 等 /api/ready，就绪后再开浏览器
├── tests/             # pytest + Node 测试 + 导出黄金样本
├── requirements.txt
├── requirements-dev.txt
├── start.bat          # Windows 一键启动
├── run.sh             # macOS / Linux 启动
└── LICENSE
```

---

## 🌐 HTTP 与 WebSocket 协议（v2）

### HTTP

| 路径 | 返回 | 用途 |
|------|------|------|
| `/api/health` | `{"ok":true}` | **仅表示进程存活**，不反映模型状态 |
| `/api/ready` | `{ready, model, error, retrying, attempts}` | **就绪探针**，前端门控只用它 |
| `/api/status` | 同 `/api/ready` | 兼容旧调用 |
| `/api/config` | 模型、语言、帧长、时长上限等 | 前端初始化 |
| `POST /api/retry` | 同 `/api/ready` | 模型加载失败后重试（无需重启进程） |

### WebSocket

客户端 → 服务端：

| 消息 | 说明 |
|------|------|
| 二进制帧 | Int16 PCM（16 kHz 单声道），≥100 ms / ≤32 KiB |
| `{"type":"config","language":"zh","epoch":1,"origin_ms":0}` | 语言 + 时间轴原点 |
| `{"type":"flush"}` | 结束当前句子并立即转写 |

服务端 → 客户端：

| 消息 | 说明 |
|------|------|
| `{"type":"status","protocol":"2","ready":true,...}` | 握手状态与变更推送 |
| `{"type":"partial","text":"...","epoch":1,"start":1.2}` | 实时字幕（会被覆盖） |
| `{"type":"final","id":"1-1","epoch":1,"text":"...","start":1.2,"end":3.8}` | 确定文本，`id` 为 `epoch-序号`，全局唯一 |
| `{"type":"error","code":"busy","message":"...","retryable":true}` | 错误（`frame_too_large` / `busy` / `model_not_ready` / `unsupported_protocol`） |
| `{"type":"warning","code":"audio_backlog_dropped","dropped_ms":320}` | 降级告警，不中断 |
| `{"type":"done"}` | 会话结束 |

---

## ❓ 常见问题

<h3 id="troubleshooting">排错</h3>

**麦克风没有反应？**
浏览器要求安全上下文：必须通过 `http://localhost`、`http://127.0.0.1` 或 HTTPS 访问。用局域网 IP 访问时需要 HTTPS。

**模型一直显示「加载中」？**
首次启动需要下载模型（约 490 MB）。检查网络；国内建议设置 `HF_ENDPOINT=https://hf-mirror.com`。也可以访问 `/api/ready` 看真实状态——`/api/health` 只代表进程活着。

**模型加载失败后必须重启吗？**
不需要。点界面上的「重试」即可；失败后系统还会自动重试 3 次（间隔 60 s），之后保留手动重试入口。

**识别速度慢？**
换用更小的模型（`ECHONOTE_MODEL=base`），或关闭其他占用 CPU 的程序。延迟口径见上文，请按 A/B/C 档对照。

**识别不准确？**
在左上角选择明确的语言（如「中文」）通常比「自动识别」更准；环境噪声较大时可适当调高 `ECHONOTE_VAD_THRESHOLD`（数值越大越不灵敏），或使用带降噪的麦克风。

**录音很久会怎样？**
默认 90 分钟软上限：72 分钟提示，到点自动停止，但已产生的文字与音频全部可以导出，不会静默丢弃。

**支持系统声音录制吗？**
当前版本录制麦克风。系统声音需要浏览器 `getDisplayMedia` 授权，计划在后续版本支持。

---

## 🧪 开发与测试

```bash
pip install -r requirements-dev.txt

# Python：单元 / 契约 / 集成（不需要模型，也不联网）
python -m pytest -q --cov=app

# 前端：WAV 头与导出黄金样本
node --test tests/js/export.test.mjs tests/js/wav.test.mjs

# 语法检查
python -m compileall -q app tests tools
node --check web/app.js
```

覆盖率门槛（CI 强制）：`app/vad.py` ≥90%、`app/stt.py` ≥70%、`app/main.py` ≥60%。
CI 矩阵为 Python 3.10/3.11/3.12/3.13（Linux）+ Windows 3.10；**不支持 3.9**（2025-10 已 EOL）。

---

## 🗺 Roadmap

- [ ] 延迟达标（A 档 p95）与基准集 `zh-bench-v1`
- [ ] 系统声音 / 双轨录制
- [ ] 说话人分离（speaker diarization）
- [ ] 本地历史记录与搜索（需先做隐私评审）
- [ ] 模型热切换
- [ ] 桌面端打包（Tauri）
- [ ] 英文界面

---

## 📝 相关文章

- 中文：[我用 faster-whisper 做了一个本地实时语音转文字工具](docs/articles/juejin.md)
- English：[Building an Open-Source Local Recorder with Real-Time Speech-to-Text](docs/articles/devto.md)

---

## 📄 License

[MIT](LICENSE) © 2026 Qsianuo

<div align="center">

**录音与识别，都在你自己的设备上。** 🎧

</div>
