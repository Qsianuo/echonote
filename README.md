<div align="center">

# 🎙 EchoNote · 声记

**本地运行的开源录音 + 实时语音转文字工具**

[![Python](https://img.shields.io/badge/Python-3.9+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Whisper](https://img.shields.io/badge/faster--whisper-local%20STT-412991?style=flat-square&logo=openai&logoColor=white)](https://github.com/SYSTRAN/faster-whisper)
[![License](https://img.shields.io/badge/License-MIT-64ffda?style=flat-square)](LICENSE)

边说边出字 · 录音不上传 · 离线可用 · 支持中英日韩等多语言

</div>

---

## ✨ 功能

- 🎙 **一键录音** —— 浏览器麦克风采集，16 kHz 单声道 PCM
- ⚡ **实时转写** —— 边说边出字，停顿后自动断句（近实时，延迟约 1–2 秒）
- 🔒 **完全本地** —— 语音识别使用本地 faster-whisper 模型，音频不上传任何服务器
- 🌍 **多语言** —— 中文 / 英文 / 日文 / 韩文 / 法文 / 德文 / 西班牙文 / 俄文，可自动识别
- 📝 **三种文本导出** —— TXT 文本、Markdown 笔记、SRT 字幕
- 💾 **录音导出** —— 一键下载 16 kHz WAV 原始录音
- 🎨 **深色界面** —— 录音状态、实时波形、计时器、自动滚动字幕
- ⌨️ **快捷键** —— 空格键开始 / 停止

> 截图：`docs/screenshot.png`

---

## 🚀 快速开始

### Windows（推荐）

1. 安装 [Python 3.9+](https://www.python.org/downloads/)（安装时勾选 **Add Python to PATH**）
2. 双击 **`start.bat`**
3. 浏览器自动打开 `http://127.0.0.1:8000`
4. 等待顶部「模型就绪」，点击麦克风开始录音

首次运行会自动创建虚拟环境、安装依赖并下载语音模型（约 150–500 MB，取决于模型大小）。

### macOS / Linux

```bash
chmod +x run.sh
./run.sh
```

### 手动启动

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate

pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

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

然后重新启动。如果你有本地代理，也可以先设置：

```bash
set HTTPS_PROXY=http://127.0.0.1:7897
set HTTP_PROXY=http://127.0.0.1:7897
```

---

## ⚙️ 配置

所有配置通过环境变量覆盖，无需改代码：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ECHONOTE_MODEL` | `small` | 模型：`tiny` / `base` / `small` / `medium` / `large-v3` |
| `ECHONOTE_MODEL_DIR` | `./models` | 模型缓存目录 |
| `ECHONOTE_LANGUAGE` | `zh` | 默认语言，如 `zh`、`en`；设为 `auto` 自动识别 |
| `ECHONOTE_VAD_THRESHOLD` | `0.010` | 语音检测灵敏度（越小越灵敏） |
| `ECHONOTE_VAD_HANGOVER_MS` | `650` | 停顿多久算一句话结束 |
| `ECHONOTE_PARTIAL_INTERVAL_S` | `1.2` | 实时字幕刷新间隔（秒） |
| `ECHONOTE_HOST` | `127.0.0.1` | 监听地址 |
| `ECHONOTE_PORT` | `8000` | 端口 |

### 模型选择建议

| 模型 | 体积 | 速度 | 中文准确率 | 适合 |
|------|------|------|-----------|------|
| `tiny` | ~75 MB | 最快 | 一般 | 低配试水 |
| `base` | ~145 MB | 快 | 可用 | 普通配置 |
| **`small`** | ~490 MB | 适中 | **好** | **默认推荐** |
| `medium` | ~1.5 GB | 慢 | 很好 | 高配 / 高精度 |

> 纯 CPU 即可运行（int8 量化）。独立显卡不是必需的。

---

## 🔧 工作原理

```text
浏览器麦克风
   │  getMediaStream + AudioWorklet
   ▼
16 kHz 单声道 PCM ──WebSocket──▶ FastAPI
                                  │
                                  ▼
                        能量 VAD 自动断句
                         │              │
                  （说到一半）      （停顿 ≥ 0.65s）
                         │              │
                         ▼              ▼
                   faster-whisper 转写
                         │              │
                         ▼              ▼
                    partial 字幕    final 字幕
                         └──────┬───────┘
                                ▼
                          浏览器实时显示
```

- **分段识别**：按停顿切分语音，避免整段长音频等待
- **部分结果**：说话过程中约每 1.2 秒刷新一次字幕
- **最终结果**：停顿后生成确定文本，并记录时间戳
- **本地推理**：`faster-whisper` + CTranslate2 int8，CPU 友好

---

## 📤 导出格式

| 格式 | 内容 |
|------|------|
| **WAV** | 16 kHz / 16-bit / 单声道原始录音 |
| **TXT** | 带时间戳的纯文本 |
| **Markdown** | 适合直接粘贴到笔记 / 博客 |
| **SRT** | 标准字幕文件，可导入剪辑软件 |

---

## 🔒 隐私

- 音频只在本机的 `127.0.0.1` 与服务端通信，**不经过互联网**
- 识别使用本地模型，**不需要 API Key**
- 录音与转写不会写入磁盘（导出的文件由你自己保存）
- 无遥测、无统计、无第三方请求（模型下载除外）

---

## 🧩 项目结构

```text
echonote/
├── app/
│   ├── main.py        # FastAPI 服务、WebSocket 会话管理
│   ├── stt.py         # faster-whisper 模型加载与转写
│   ├── vad.py         # 能量 VAD 自动断句
│   └── config.py      # 环境变量配置
├── web/
│   ├── index.html     # 界面
│   ├── style.css      # 深色主题
│   ├── app.js         # 录音、实时字幕、导出逻辑
│   └── pcm-worklet.js # 音频重采样 Worklet
├── requirements.txt
├── start.bat          # Windows 一键启动
├── run.sh             # macOS / Linux 启动
└── LICENSE
```

---

## 🌐 WebSocket 协议

客户端 → 服务端：

| 消息 | 说明 |
|------|------|
| 二进制帧 | Int16 PCM（16 kHz 单声道），每帧约 40–90 ms |
| `{"type":"config","language":"zh"}` | 切换识别语言 |
| `{"type":"flush"}` | 结束当前句子并立即转写 |

服务端 → 客户端：

| 消息 | 说明 |
|------|------|
| `{"type":"status","ready":true,"model":"small"}` | 模型状态 |
| `{"type":"partial","text":"..."}` | 实时字幕（会被后续结果替换） |
| `{"type":"final","id":1,"text":"...","start":1.2,"end":3.8}` | 确定文本 |
| `{"type":"done"}` | 会话结束 |

---

## ❓ 常见问题

**麦克风没有反应？**
浏览器要求安全上下文：必须通过 `http://localhost`、`http://127.0.0.1` 或 HTTPS 访问。用局域网 IP 访问时需要 HTTPS。

**模型一直显示“加载中”？**
首次启动需要下载模型。检查网络；国内建议设置 `HF_ENDPOINT=https://hf-mirror.com`。模型加载完成后状态会变成「模型就绪」。

**识别速度慢？**
换用更小的模型（`ECHONOTE_MODEL=base`），或关闭其他占用 CPU 的程序。实时字幕依赖 CPU 推理速度。

**识别不准确？**
在左上角选择明确的语言（如「中文」）通常比「自动识别」更准；环境噪声较大时调低 `ECHONOTE_VAD_THRESHOLD` 的灵敏度或使用带降噪的麦克风。

**支持系统声音录制吗？**
当前版本录制麦克风。系统声音需要浏览器 `getDisplayMedia` 授权，计划在后续版本支持。

---

## 🗺 Roadmap

- [ ] 系统声音 / 双轨录制
- [ ] 说话人分离（speaker diarization）
- [ ] 本地历史记录与搜索
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
