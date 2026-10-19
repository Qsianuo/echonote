# 我用 faster-whisper 做了一个本地实时语音转文字工具，代码已开源

> 一个不需要 API Key、不上传云端、边说边出字的开源录音软件。

![EchoNote 界面](https://raw.githubusercontent.com/Qsianuo/echonote/main/docs/screenshot.png)

平时记笔记、开会、采访，最麻烦的往往不是"录音"，而是录完之后还要花一两个小时把语音转成文字。

市面上的方案大致分两类：

- **在线工具**：上传音频 → 云端识别，准确率高，但要联网、有隐私顾虑，免费额度也有限；
- **本地软件**：隐私好，但很多需要付费，或者只支持英文。

于是我想：能不能做一个**完全本地运行、免费开源、边说边出字**的录音工具？

这就是 **EchoNote · 声记**。

- 项目地址：https://github.com/Qsianuo/echonote
- License：MIT
- 技术栈：FastAPI + faster-whisper + WebSocket + 原生 JavaScript

---

## 一、先看效果

打开网页，点击麦克风，开始说话。屏幕上会先出现**灰色的实时字幕**，停顿之后变成**正式的黑色文本**，并带上时间戳。

录完之后可以导出：

| 格式 | 用途 |
|------|------|
| WAV | 16kHz 原始录音 |
| TXT | 带时间戳的纯文本 |
| Markdown | 直接粘贴到笔记/博客 |
| SRT | 导入剪映、PR 等剪辑软件做字幕 |

整个过程，音频只在本机的 `127.0.0.1` 上传输，**不经过互联网**，也不需要任何 API Key。

---

## 二、技术选型：为什么是 faster-whisper + Web

### 1. 为什么不用在线语音识别 API？

- **隐私**：会议、采访内容不适合上传第三方；
- **成本**：长期使用有费用；
- **网络**：离线场景（比如出差、断网）无法使用。

### 2. 为什么用本地 Whisper？

OpenAI 的 Whisper 是目前开源语音识别里综合表现最好的模型之一，支持多语言，中文效果也不错。

但原生 Whisper 基于 PyTorch，在普通笔记本上跑起来偏慢。**faster-whisper** 使用 CTranslate2 重新实现了推理，并支持 int8 量化，**纯 CPU 就能跑**，速度比原版快很多，内存占用也更低。

我的笔记本是 i5-8300H + GTX 1050 Ti，用 `small` 模型跑实时转写完全没有压力。

### 3. 为什么做成 Web 应用，而不是 Electron？

| 方案 | 优点 | 缺点 |
|------|------|------|
| Electron | 桌面体验 | 打包体积大，音频设备 API 复杂 |
| 浏览器 + 本地服务 | 跨平台、开发快、UI 好做 | 需要在浏览器里打开 |

浏览器本身就有成熟的 `getUserMedia`、`AudioWorklet`、`WebSocket`，做实时音频再合适不过。用户只需要双击一个 `start.bat`，浏览器自动打开，体验和桌面软件差别不大。

---

## 三、整体架构

```text
浏览器麦克风
   │  getUserMedia + AudioWorklet
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

核心思路只有一句话：**浏览器负责采集和展示，Python 负责断句和识别，两者用 WebSocket 传 PCM 数据。**

---

## 四、核心实现

### 1. 浏览器端：把麦克风变成 16kHz PCM

很多教程会用 `MediaRecorder` 录成 WebM 再传给后端。但 WebM 在服务端解码需要 ffmpeg，依赖重，还不好做流式处理。

更好的做法是直接用 **AudioWorklet** 拿到原始采样：

```js
const ctx = new AudioContext({ sampleRate: 16000 });
await ctx.audioWorklet.addModule('pcm-worklet.js');

const source = ctx.createMediaStreamSource(stream);
const worklet = new AudioWorkletNode(ctx, 'pcm16');
source.connect(worklet);
worklet.port.onmessage = (event) => {
  const chunk = new Int16Array(event.data);
  ws.send(event.data); // 直接发送二进制 PCM
};
```

Worklet 里把 Float32 转成 Int16，并做一次线性重采样，保证无论浏览器给的是什么采样率，后端收到的都是 16kHz：

```js
class PCM16Processor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.buffer = new Float32Array(0);
    this.pos = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) return true;

    const merged = new Float32Array(this.buffer.length + channel.length);
    merged.set(this.buffer);
    merged.set(channel, this.buffer.length);

    const out = [];
    while (this.pos + 1 < merged.length) {
      const i = Math.floor(this.pos);
      const frac = this.pos - i;
      const sample = merged[i] * (1 - frac) + merged[i + 1] * frac;
      out.push(sample < 0 ? sample * 0x8000 : sample * 0x7fff);
      this.pos += this.ratio;
    }

    if (out.length) {
      this.port.postMessage(new Int16Array(out).buffer, [/* transfer */]);
    }
    // 保留未消费的尾部
    const keepFrom = Math.floor(this.pos);
    this.buffer = merged.slice(keepFrom);
    this.pos -= keepFrom;
    return true;
  }
}
```

这样服务端拿到的就是**干净、统一、无需解码**的 PCM。

### 2. 自动断句：能量 VAD

Whisper 不是流式模型，整段长音频一次性识别会等很久，而且没有"实时感"。

我用的方案是：**按说话停顿自动分段**，每次只把一句话送给模型。

判断逻辑很朴素——计算每 30ms 音频帧的 RMS（能量），和一个自适应噪声底比较：

```python
rms = np.sqrt(np.mean(np.square(frame)))
threshold = max(0.010, noise_floor * 3.0)
is_voice = rms > threshold
```

状态机大致是这样：

- 连续有声 → 进入"说话中"，开始缓存；
- 静音超过 **650ms** → 认为这句说完了；
- 有效语音太短（< 280ms）→ 丢弃，避免键盘声误触发；
- 单句超过 15 秒 → 强制切分，避免内存和延迟失控。

说话过程中，每 **1.2 秒**把当前缓冲送给模型一次，得到 `partial` 字幕；停顿之后再生成 `final` 字幕。

这样既保证了实时感，又保证了最终文本的完整度。

### 3. 服务端：faster-whisper 推理

模型只在启动时加载一次：

```python
model = WhisperModel(
    "small",
    device="cpu",
    compute_type="int8",
    download_root="./models",
)

segments, info = model.transcribe(
    audio,
    language="zh",
    beam_size=1,
    condition_on_previous_text=False,
    without_timestamps=True,
    initial_prompt="以下是普通话的句子，请使用简体中文输出，并添加标点符号。",
)
text = "".join(s.text for s in segments).strip()
```

几个关键参数：

- `compute_type="int8"`：CPU 推理提速明显，准确率损失很小；
- `beam_size=1`：实时场景优先速度；
- `condition_on_previous_text=False`：避免分段识别时上下文串味；
- `initial_prompt`：**强制输出简体中文和标点**，否则 Whisper 经常会输出繁体、不带标点。

### 4. WebSocket 协议

浏览器和服务端之间只有几种消息：

| 方向 | 消息 | 说明 |
|------|------|------|
| C → S | 二进制帧 | 16kHz Int16 PCM |
| C → S | `{"type":"config","language":"zh"}` | 切换语言 |
| C → S | `{"type":"flush"}` | 结束当前句子，立即转写 |
| S → C | `{"type":"partial","text":"..."}` | 实时字幕（会被替换） |
| S → C | `{"type":"final","id":1,"text":"...","start":1.2,"end":3.8}` | 最终文本 |

前端渲染时，`partial` 永远显示在最后一行，收到 `final` 就把它变成正式段落。这样用户看到的就是"边说边改写"的效果。

### 5. 录音导出

浏览器端保存了所有 PCM 分片，导出 WAV 只需要拼一个 44 字节的 WAV 头：

```js
const total = chunks.reduce((n, c) => n + c.length, 0);
const buffer = new ArrayBuffer(44 + total * 2);
// 写入 RIFF / WAVE / fmt / data 头...
chunks.forEach((chunk) => {
  new Int16Array(buffer, offset, chunk.length).set(chunk);
  offset += chunk.length * 2;
});
```

不需要后端参与，也没有额外的音频编码开销。

---

## 五、踩过的坑

### 1. Whisper 输出繁体、没有标点

一开始语音转出来是"這是回聲筆記的實時識別測試"，用户看着很难受。

解决方式就是上面提到的 `initial_prompt`，填入一句简体中文示例。模型会沿用这个 prompt 的语言风格和标点习惯。

### 2. 自动识别语言不一定是想要的结果

Whisper 的自动语言检测在中文场景下可能输出繁体。对于中文用户，直接默认 `zh` 反而更稳。EchoNote 现在默认中文，也可以在界面切换成 English、日本語、한국어 等。

### 3. 模型下载慢/失败

faster-whisper 首次运行会从 Hugging Face 下载模型。国内网络不稳定，可以设置镜像：

```bash
set HF_ENDPOINT=https://hf-mirror.com
```

如果还是慢，可以手动下载模型文件，放在本地目录，然后把 `ECHONOTE_MODEL` 指向它。

### 4. 分段太短影响准确率

一开始每 2 秒强行切一段，结果识别出来的句子支离破碎。

后来改成"停顿才切"，用 VAD 保证一段话至少包含完整语义，最长不超过 15 秒，准确率明显提升。

### 5. 浏览器要求安全上下文

麦克风 API 只在 `localhost` 或 HTTPS 下可用。所以应用默认绑定 `127.0.0.1`，不要直接用局域网 IP 访问。

---

## 六、怎么运行

```bash
git clone https://github.com/Qsianuo/echonote.git
cd echonote
```

Windows 直接双击 `start.bat`，macOS/Linux 运行 `./run.sh`。

首次启动会自动创建虚拟环境、安装依赖、下载模型，之后打开：

```
http://127.0.0.1:8000
```

模型选择建议：

| 模型 | 体积 | 中文效果 | 适合 |
|------|------|----------|------|
| tiny | 75MB | 一般 | 低配试水 |
| base | 145MB | 可用 | 普通配置 |
| **small** | 490MB | **好** | **推荐** |
| medium | 1.5GB | 很好 | 高配 |

---

## 七、还可以做什么

这个项目目前还是一个 MVP，后面计划加：

- [ ] 系统声音 / 双轨录制
- [ ] 说话人分离（谁在说）
- [ ] 本地历史记录与搜索
- [ ] 模型热切换
- [ ] 桌面端打包（Tauri）

---

## 八、最后

做这个项目最大的感受是：**浏览器 + 本地 Python 服务的组合，比想象中强大**。

不需要 Electron 的臃肿，也不需要云服务的成本，就能做出一个体验接近商业软件的本地工具。

如果你也在做语音相关的东西，或者需要一个离线转写工具，欢迎试试：

**https://github.com/Qsianuo/echonote**

如果对你有帮助，点个 Star 就是最大的鼓励 ⭐

也欢迎在 Issues 里交流：实时分段策略、模型选型、中文识别优化，都可以聊。

---

**标签**：语音识别 · Whisper · Python · FastAPI · WebSocket · 开源项目
