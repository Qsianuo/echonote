<div align="center">

# 🎙 EchoNote · 声记

**本地运行的开源录音 + 实时语音转文字工具**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Whisper](https://img.shields.io/badge/faster--whisper-local%20STT-412991?style=flat-square&logo=openai&logoColor=white)](https://github.com/SYSTRAN/faster-whisper)
[![License](https://img.shields.io/badge/License-MIT-64ffda?style=flat-square)](LICENSE)
[![Stage](https://img.shields.io/badge/stage-v0.12.0%20tech%20preview-f0b429?style=flat-square)](#-延迟口径重要)

边说边出字 · 录音不上传 · 装好模型后完全离线 · 支持中英日韩等多语言

</div>

---

> **这是 v0.12.0 技术预览（tech preview），不是 v1.0.0。**
> 三件事需要先说清楚：**实验性**；**延迟取决于硬件**（本机实测首字 2.8 s、停顿后 1 s 出定稿、约 12 s 完成精修，见下文口径）；**首次准备要下载两个模型**（145 MB + 1.5 GB），之后可完全断网运行。

---

## ✨ 功能

- 🎙 **一键录音** —— 浏览器麦克风采集，16 kHz 单声道 PCM
- ⚡ **实时转写** —— 边说边出字，停顿后自动断句
- 🔒 **完全本地** —— 语音识别使用本地 faster-whisper 模型，音频不上传任何服务器
- ⚡ **边说边出字** —— 两个模型分工：`base` 边说边画实时字幕，`medium` 在每句结束后重写定稿（本机样本字符错误率 0）；界面里可随时切换档位
- 🔌 **真正离线** —— 模型在磁盘上时，识别全程零联网；`--offline` 还能强制断网启动
- 🌍 **多语言** —— 中文 / 英文 / 日文 / 韩文 / 法文 / 德文 / 西班牙文 / 俄文，可自动识别
- 📝 **四种导出** —— TXT 文本、Markdown 笔记、SRT 字幕、WAV 原始录音
- 💾 **边录边落盘** —— 音频分片同步写入浏览器 OPFS，录音期间内存占用与时长无关；OPFS 不可用（隐私模式等）时自动降级为内存，界面一行提示
- ▶️ **回放与字幕联动** —— 停止后可回放本次录音：30 秒窗口解码、点任意字幕跳转到该句并高亮，播放条激活时空格键控制播放（不再误触录音）
- ⏱ **逐段延迟埋点** —— 每句从「说完」到「首字 / 定稿 / 精修」的毫秒数逐段上报，瓶颈在哪一段一看就知道
- 🎨 **深色界面** —— 录音状态、实时波形、计时器、自动滚动字幕
- ⌨️ **全键盘可用** —— 空格开始 / 停止，导出菜单支持方向键与 Esc
- ♿ **读屏友好** —— 状态与字幕通过 live region 播报，错误使用 `role="alert"`

![EchoNote 界面](docs/screenshot.png)

> 截图取自 v0.10.0 打包版实际运行（离线模式 + 8.95 s 中文样例音频）。v0.11 的界面在模型标签上显示「实时 极速 · 定稿 快速」，
> 并为定稿过程加了「精修中 / 已精修」标记。全套流程见
> `docs/screenshots/`：空态 → 录音中 → 停止后字幕 → 导出菜单 → 窄屏。
> 该次录音导出的四种文件在 `docs/examples/`。

---

## 📥 下载即用（Windows，不用装 Python）

去 [Releases](https://github.com/Qsianuo/echonote/releases) 下载最新的
`EchoNote-*-windows-x64.zip`，解压后双击 `EchoNote.exe` 即可。

1. 解压到**任意可写目录**（例如 `D:\EchoNote`），不要放 `C:\Program Files`
2. 双击 **`fetch-model.bat`** 准备模型（实时字幕 145 MB + 定稿 1.5 GB，自动走国内镜像）
3. 双击 `EchoNote.exe`，模型就绪后浏览器自动打开，点麦克风开始录音
4. 关闭命令行窗口即退出

> 已有离线模型包的，把 `models\` 整个目录放进解压目录（与 `EchoNote.exe` 同级）即可跳过第 2 步。

排查问题用自检命令，会把原生依赖、前端资源、模型状态逐项列出来：

```bat
EchoNote.exe --self-test
```

> 绿色版，不写注册表、不留后台服务。录音与识别全在本机，音频不会离开电脑。

---

## 🔌 离线使用

模型一旦落在 `models\` 里，**识别过程不会再发任何网络请求** —— 不是"缓存可能命中"，而是加载时直接拿本地目录，faster-whisper 根本没有机会去查 Hub。

三种离线场景：

| 场景 | 做法 |
|---|---|
| 先联网准备一次，之后断网 | 跑一次 `fetch-model.bat`，或 `python tools/fetch_model.py` |
| 完全隔离的机器 | 在有网的机器上下载 `models\` 目录，拷到目标机器 exe 同级 |
| 需要硬保证不联网 | `EchoNote.exe --offline`（或 `ECHONOTE_OFFLINE=1`），缺模型时立刻报错并给出应放路径，不会卡在下载上 |

验证离线是否真的生效：

```bat
EchoNote.exe --self-test
```

输出里 `model on disk : yes` 表示权重已在本机；界面上出现绿色的 **「离线可用」** 角标，表示当前进程加载的就是本地权重。

界面里的 `识别模型` 下拉框可以随时切换已安装的档位，切换只加载本地文件，**不会触发下载**。

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

**① 每次调用的成本几乎是常数。** Whisper 会把音频补齐到 30 s 再编码，所以一次 `transcribe()` 的耗时与音频长短关系很小。开发机（i5-8300H，4 核 8 线程，纯 CPU int8）上 `base` 每次调用 1.0–1.4 s，`small` 约 1.1 s（9 s 音频）/ 1.2 s（1.5 s 音频）。

**② 所以「切碎了喂」是最差的做法。** v0.10 用 `large-v3-turbo` 把录音切成 3 s 一句、每句调一次，固定开销叠了十几倍，实测 RTF 3.9 —— 说一句要等十几秒。

v0.11 换掉的就是这个结构：**VAD 只负责判断「有人在说话」，音频连续流入一个增长中的缓冲**，实时字幕按增量解码，句子结束再定稿。

| 环节 | 用什么模型 | 本机实测 |
|---|---|---|
| 首条字幕出现 | `base`（极速，145 MB） | **2.8–3.4 s**（首帧起算，含 VAD 起步 2 s 缓冲） |
| 说话过程中刷新 | `base` | 每 ~0.7–1.4 s 一次，逐字追加 |
| 停顿后定稿 | `base`（复用实时结果） | **0.0–1.1 s** |
| 定稿精修 | `medium`（均衡，1.5 GB） | **~12 s**（后台，不挡字幕） |

**准确性优先。** 同一段 8.95 s 样本，用应用真实的定稿参数（beam 5 + 承接上下文 + 中文 prompt）实测：

| 定稿档 | 耗时 | 字符错误率 | 输出 |
|---|---|---|---|
| `small` | 3.7 s | 3.4% | …回声笔记的**时时**语音识别测试… |
| `medium` | 12.1 s | **0.0%** | 全对 |
| `large-v3-turbo` | 15.7 s | **0.0%** | 全对 |

`medium` 与 `turbo` 在这段样本上并列满分，`medium` 快 23%、小 90 MB，所以默认用它；想要更新的蒸馏模型可在下拉框里切 `turbo`。`small` 只差一个字，但那一个字正是笔记里最要命的同音字。

代价要讲清楚：12 s/句 ≈ 2.4 倍实时，**长篇连续说话时精修会排队**。队列现在按音频秒数计（默认 240 s），超过才丢弃并提示；停止录音时会按积压量等精修落地（上限 60 s），不会让你导出半成品。

对比 v0.10 同一段 8.95 s 音频、同一台机器：首条字幕 **8.25 s → 2.8 s**，停顿后定稿 **2.2 s → 1.0 s**。

几个刻意的取舍，都写在代码注释里，这里只列结论：

- **首条字幕要等 2 s 缓冲**，不是 1 s。1.2 s 缓冲时 `base` 会把「你好，这是回声笔记」读成「以后，这是」—— 少等 1 s，用户先看到一句错话。
- **置信度门限是量出来的**（`LIVE_MIN_LOGPROB = -1.2`）。1.2–2.0 s 缓冲的真实读数落在 −1.09 ~ −0.33，最初拍的 −0.85 把正确字幕全丢了。
- **短缓冲会被丢弃**：句尾解码若赶不上句尾，就直接重解一次整句，而不是等一个覆盖不到句尾的在途解码（那会白等 1.6 s）。
- **小模型吃词表，不吃 prompt**：`tiny` 会把指令 prompt 当问题来回答（界面上出现「请使用简体中文和标点符号转写。」）；换成短提示词「以下是普通话录音。」后繁体消失、也不再复读。真正提升准确率的是「专有名词」——同一段样本 CER 27.6% → 13.8%。
- **精修会让路**：只要还在说话、或有定稿在途，精修线程就不启动 —— 两个模型抢 4 个核，会把 0.9 s 的解码拖成 3.9 s。

- 服务端把实测值放进状态：界面显示「实时字幕每次解码约 N 秒」，不用你自己算。
- **逐段延迟可以自己量**：每句话说完之后，服务端会发一条 `timing` 消息（见协议段），带上到「首字 / 定稿 / 精修」各花了多少毫秒。上表的数字是本机一次实测，不是保证值——你的硬件、模型档位、句长都会改变它，`timing` 就是给这件事用的口径。
- 「识别模型」下拉框可随时切换，切换只加载本地文件，**不会触发下载**。
- A/B/C 档的 p95 门禁（首条 partial ≤2.0 s、final ≤3.0 s）**仍未达标**：首字 2.8 s 卡在「2 s 缓冲 + 1 次解码」的物理下限，要再快只能换更强的 CPU 或更小的模型。这是技术预览的原因之一。

---

## 🌐 模型下载与国内网络

模型托管在 Hugging Face。`fetch-model.bat` 与 `python tools/fetch_model.py` 默认就走 `https://hf-mirror.com`，并且关掉了镜像无法代理的 Xet 通道：

```bash
# 默认双档：极速 145 MB（实时字幕）+ 快速 464 MB（定稿）
python tools/fetch_model.py

# 指定档位 / 换源 / 强制重下
python tools/fetch_model.py --model small
python tools/fetch_model.py --endpoint https://huggingface.co
python tools/fetch_model.py --force

# 查看有哪些档、哪些已经装好
python tools/fetch_model.py --list
```

也可以手工把任意转换好的模型目录放到 `models/<名字>/`（需含 `model.bin` 与 `config.json`），再用 `ECHONOTE_MODEL=<目录>` 指过去。

---

## ⚙️ 配置

所有配置通过环境变量覆盖，无需改代码：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ECHONOTE_MODEL` | `medium` | 定稿档位：`base` / `small` / `medium` / `large-v3-turbo` / `large-v3`，或任意本地目录 |
| `ECHONOTE_MODEL_DIR` | `./models` | 模型目录 |
| `ECHONOTE_OFFLINE` | 未设置 | 设为 `1` 后禁止一切联网；缺模型时立即报错并给出应放路径 |
| `ECHONOTE_LANGUAGE` | `zh` | 默认语言，如 `zh`、`en`；`auto` 为自动识别 |
| `ECHONOTE_VAD_THRESHOLD` | `0.010` | 能量阈值。**数值越大越不灵敏**（更难触发），越小越容易把噪声当语音 |
| `ECHONOTE_VAD_HANGOVER_MS` | `900` | 停顿多久算一句结束。按 30 ms 取整，**实际 900 ms** |
| `ECHONOTE_VAD_MIN_SPEECH_MS` | `240` | 最短语音，按帧取整后**实际 240 ms**，更短的片段直接丢弃 |
| `ECHONOTE_VAD_MAX_SEGMENT_S` | `30` | 单段最长时长，到点强制切分。切太短会丢上下文，反而更不准 |
| `ECHONOTE_VAD_PREROLL_MS` | `300` | **预滚**：确认「有人在说话」之前的那段音频也送进解码，避免句首轻声、清辅音被切掉。按 30 ms 取整（300 → 10 帧）；代价是字幕时间轴最多前移 300 ms |
| `ECHONOTE_BEAM_SIZE` | `5` | final 的 beam 宽度；`1` 更快但错字更多 |
| `ECHONOTE_TEMP_FALLBACK` | 未设置 | 开启温度回退阶梯。**本机实测会把 30 s 音频截断到 1/3，默认关闭** |
| `ECHONOTE_CONDITION_ON_PREVIOUS` | `1` | final 是否承接上一句上下文 |
| `ECHONOTE_PROMPT_ZH` | 见源码 | 中文 initial_prompt，用于压住繁体与无标点输出 |
| `ECHONOTE_CPU_THREADS` | `0` | ctranslate2 线程数，`0` 为自动 |
| `ECHONOTE_LIVE_MODEL` | 自动 | 实时字幕用哪个档；默认取已安装的最快一档（`base` > `small` > `tiny`），都没有就用定稿档 |
| `ECHONOTE_DISABLE_LIVE` | 未设置 | 设为 `1` 退回 v0.10 行为：没有增量字幕，每句结束才出文字 |
| `ECHONOTE_LIVE_STEP_S` | `0.6` | 实时解码的**下限节流**：至少来了这么多新音频才解一次，不是承诺值 |
| `ECHONOTE_DISABLE_REFINE` | 未设置 | 设为 `1` 关闭定稿精修，字幕只保留实时档的结果（更快，但准确率降一档） |
| `ECHONOTE_HOTWORDS` | 读 `models/hotwords.txt` | 专有名词表。优先级：环境变量 > `models/hotwords.txt` > 内置默认值 |
| `ECHONOTE_TIMING_LOG` | 未设置 | 设为 `1` 把每轮的逐段延迟额外打成一行 `[TURN_TIMING]` 日志（`timing` 消息本身始终发送） |
| `ECHONOTE_PEAK_NORMALIZE` | 未设置 | 峰值归一化，实验性，**默认关闭**。开启前请自行用同批样本对比 CER——**本项目未实测其 CER**，且可能把环境噪声一并抬起来，**不建议默认开启** |

### 专有名词表（`models/hotwords.txt`）

识别不准，九成是**同音字选错**：「焦距」听成「脚距」，「光圈」听成「光圈」以外的东西。Whisper 的解码器可以在候选之间偏向你给的词——这就是词表，实测是比调任何解码参数都管用的一招。

文件放在 `models/hotwords.txt`，一行一个词，`#` 开头是注释，改完**下一次解码就生效**，不用重启。界面里的「专有名词」输入框改的是同一个值。

两条限制是量出来的，不是猜的：

1. **约 52 个词封顶。** faster-whisper 会把 hotwords 截断到 224 个 token，而且**静默丢弃**尾部。52 个摄影词 = 171 字符 = 221 token，正好在界内；超了不会报错，只会悄悄失效。
2. **只放你真的会说的词。** 词表是解码提示，小模型会顺着它往下续写。填了音频里不会出现的词，它反而在句子里插字——实测多放一个「语音识别」，句子里稳定多出一个「课」。词表与录音内容不匹配时，`tiny` 还会陷入复读循环（`记记记记…`），已加防复读闸兜底。

仓库里 `models/hotwords.txt` 是一份摄影讲课的词表（相机/镜头/打光/焦距/光圈/曝光那一套），照着这个格式换成你的领域即可。
| `ECHONOTE_REFINE_BACKLOG_S` | `240` | 精修积压的音频秒数上限，超过才丢弃最旧的一条并提示 |
| `ECHONOTE_REFINE_DRAIN_MAX_S` | `60` | 停止录音后等待精修完成的上限 |
| `ECHONOTE_MAX_RECORD_MINUTES` | `90` | 录音时长软上限；`0` 表示不限（界面会提示内存风险自负） |
| `ECHONOTE_HOST` | `127.0.0.1` | 监听地址（启动脚本会让它生效） |
| `ECHONOTE_PORT` | `8000` | 端口（启动脚本会让它生效） |
| `ECHONOTE_ALLOW_NONLOCAL` | 未设置 | 允许非回环监听。必须同时设置令牌，否则拒绝启动 |
| `ECHONOTE_ACCESS_TOKEN` | 空 | 非回环部署时的访问令牌，WebSocket 需带 `?token=` |
| `ECHONOTE_ALLOWED_ORIGINS` | 空 | 额外允许的 Origin，逗号分隔 |

### 模型选择建议

| 模型 | 体积 | 整段 RTF（9 s） | 中文表现 | 适合 |
|------|------|----------------|---------|------|
| **`tiny`** | ~75 MB | 0.14 | 本机样本 CER 27.6%，**填词表后 13.8%** | 体积优先；配合「专有名词」才能用 |
| **`base`** | ~145 MB | 0.12 | 本机样本 CER 20.7%，错字较多 | **实时字幕默认档**；单次解码 ~1.1 s |
| `small` | ~464 MB | 0.41 | 本机样本 CER 3.4%（定稿参数下），「实时」听成「时时」 | 4 核且要精修跟得上说话 |
| **`medium`** | ~1.5 GB | 1.08 | 本机样本 CER **0.0%** | **定稿默认档**；单句精修 ~12 s |
| `large-v3-turbo` | ~1.6 GB | 1.50 | 本机样本 CER **0.0%**，与 medium 并列 | 想要更新的蒸馏模型；单句 ~16 s |
| `large-v3` | ~3.1 GB | 未测 | 理论最好 | 8 核以上再考虑 |

> 纯 CPU 即可运行（int8 量化），独立显卡不是必需的。int8 与 float32 在本机样本上输出**逐字相同**，只是慢 40%，所以量化不是准确率的瓶颈，模型规模才是。
>
> 实时字幕用 `base`、定稿用 `medium`，是刻意分工：实时那句只活几秒就被重写，错一个字的代价远小于晚一秒出字；而定稿是你要读、要导出的那一份，所以用最准的。
>
> 这个 CER 是在**一段**人工核对过的音频上算的，用于横向比较三个模型，不代表通用中文准确率。完整基线需要 ≥30 句人工标注录音（见 `docs/consistency-checklist.md`）。

---

## 🧱 使用边界

| 项 | 值 |
|----|----|
| 上行音频 | Int16 PCM / 16 kHz / 单声道，**合并后每帧 ≥100 ms**，服务端约 10 帧/秒 |
| 单帧上限 | 32 KiB，超出即断开并返回 `frame_too_large` |
| 服务端积压 | 最多缓存 60 s 音频，溢出时**丢最旧、保最新**并回 `warning`（不静默） |
| 并发会话 | 单进程最多 2 个，第 3 个收到 `busy` |
| 录音时长 | 默认软上限 90 分钟，72 分钟提示；到上限自动停止但保留全部文字与音频 |
| 落盘 | 录音期间音频分片由浏览器同步写入自己的站点存储（OPFS），停止时把 `.pcm.tmp` 改名为 `.pcm`；服务端**不写磁盘**。强关标签页会残留 `.pcm.tmp`，下次启动**只提示、不删除、不改名**。当前版本没有历史列表，导出请在本次会话内完成 |

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
     （说到一半）      （停顿 ≥900 ms）
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
- 转写文本**不写入磁盘**；录音分片只落在浏览器自己的站点存储（OPFS）里，服务端不保存音频，也不使用 IndexedDB、不写临时文件（导出的文件由你自己保存）
- 无遥测、无统计、无第三方请求（仅首次模型下载）
- 日志不记录转写文本

---

## 🧩 项目结构

```text
echonote/
├── app/
│   ├── __main__.py    # python -m app（含监听地址安全检查）
│   ├── desktop.py     # 便携版入口：横幅、自动开浏览器、--self-test
│   ├── main.py        # FastAPI、WebSocket 会话、并发与队列上限
│   ├── protocol.py    # 协议 v4 消息构造与校验
│   ├── live.py        # 实时字幕线程：增长中的缓冲 + 最新快照解码
│   ├── stream.py      # 一句话的音频缓冲、增长策略、防回缩保护
│   ├── timing.py      # 逐段延迟埋点（说完 → 首字 / 定稿 / 精修）
│   ├── stt.py         # faster-whisper 加载（可重试）与转写
│   ├── vad.py         # 能量 VAD 自动断句（含 300 ms 预滚）
│   └── config.py      # 环境变量配置（含 frozen 路径解析）
├── web/
│   ├── index.html     # 界面
│   ├── style.css      # 深色主题
│   ├── app.js         # 录音、时间轴、实时字幕、导出编排
│   ├── audio-store.js # OPFS / 内存两套落盘实现（同接口，可测试）
│   ├── storage-worker.js # OPFS 同步写盘 Worker
│   ├── playback.js    # 回放：30 s 窗口解码与定段
│   ├── waveform.js    # 波形：几何测量缓存 + 行索引
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

## 🌐 HTTP 与 WebSocket 协议（v4）

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
| `{"type":"status","protocol":"4","ready":true,...}` | 握手状态与变更推送；含 `live_model` / `live_separate` / `live_ms` |
| `{"type":"partial","text":"...","epoch":1,"start":1.2}` | 实时字幕（会被覆盖） |
| `{"type":"final","id":"1-1","epoch":1,"text":"...","start":1.2,"end":3.8,"stable":false}` | 一句的定稿。`stable:false` 表示定稿模型还会改写它 |
| `{"type":"refine","id":"1-1","epoch":1,"text":"...","start":1.2,"end":3.8}` | 精修结果：**同 id** 替换文本，时间轴不动 |
| `{"type":"timing","epoch":1,"turn_id":"1-1","speech_finished_at":12345.68,"segments":{"to_first_text_ms":2140.5}}` | **v4 新增**：逐段延迟埋点。每轮最多两条（定稿后一条，精修后再一条）。`speech_finished_at` 是 `perf_counter` 读数，只能在同一台机器上相减，不是日期；`segments` 只出现真的发生过的里程碑（`to_first_text_ms` / `to_final_text_ms` / `to_refined_ms`），没发生就不给键，避免把「没测到」读成 `0 ms` |
| `{"type":"error","code":"busy","message":"...","retryable":true}` | 错误（`frame_too_large` / `busy` / `model_not_ready` / `unsupported_protocol`） |
| `{"type":"warning","code":"audio_backlog_dropped","dropped_ms":320}` | 降级告警，不中断 |
| `{"type":"done"}` | 会话结束 |

> v4 相比 v3 只新增了 `timing` 消息（消息集变了，所以版本号仍要跳）：它是纯诊断信息，不认识它的客户端静默忽略即可，字幕、时间轴、导出都照旧。版本号存在于 `app/protocol.py` 的 `PROTOCOL_VERSION`，前端不一致时提示「请刷新页面」。

---

## ❓ 常见问题

<h3 id="troubleshooting">排错</h3>

**麦克风没有反应？**
浏览器要求安全上下文：必须通过 `http://localhost`、`http://127.0.0.1` 或 HTTPS 访问。用局域网 IP 访问时需要 HTTPS。

**模型一直显示「加载中」？**
很可能是模型还没准备好。双击 `fetch-model.bat`（默认走国内镜像）下载一次；或在界面里点「重试」看具体错误。访问 `/api/ready` 能看到真实状态与实测倍速——`/api/health` 只代表进程活着。

**模型加载失败后必须重启吗？**
不需要。点界面上的「重试」即可；失败后系统还会自动重试 3 次（间隔 60 s），之后保留手动重试入口。

**识别速度慢？**
先看界面上的提示。v0.11 把实时字幕和定稿分开了：字幕由最快的已安装档（默认 `base`）画，定稿由「识别模型」下拉框里那一档（默认 `medium`，没装时取已安装的最优档）算。如果提示说实时解码超过 1.5 s，装一个 `base`（`fetch-model --model base`，145 MB）就能明显改善；定稿慢只会影响「精修中」标记的停留时间，不影响你读字幕。

**识别不准确？**
如果你在用 75 MB 的「试验」档：**先填「专有名词」**（空格分隔，例如 `回声笔记 二期 张总`），它走的是解码器的 `hotwords`，实测同一段样本字符错误率 27.6% → 13.8%，比调任何解码参数都管用。其余按收益排序：① 把「识别模型」（定稿档）换成更大的模型，`medium` / `large-v3-turbo` 的同音字错误明显更少；② 明确选「中文」而不是「自动识别」；③ 环境噪声大时调高 `ECHONOTE_VAD_THRESHOLD`（数值越大越不灵敏），或用带降噪的麦克风。实时字幕那一行天生比定稿差一档，最终文本以定稿/精修结果为准。

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
