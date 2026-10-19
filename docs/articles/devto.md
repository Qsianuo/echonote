---
title: "Building an Open-Source Local Recorder with Real-Time Speech-to-Text"
published: false
description: "How I built EchoNote: a privacy-first recorder that transcribes speech live using faster-whisper, FastAPI, and WebSockets — no API keys, nothing uploaded."
tags: python, ai, opensource, webdev
cover_image: https://raw.githubusercontent.com/Qsianuo/echonote/main/docs/screenshot.png
---

# Building an Open-Source Local Recorder with Real-Time Speech-to-Text

![EchoNote UI](https://raw.githubusercontent.com/Qsianuo/echonote/main/docs/screenshot.png)

Taking notes during meetings or interviews is tedious. Recording is easy — turning that audio into text is the painful part.

Cloud transcription services solve it, but they come with trade-offs: you need an internet connection, your audio leaves your machine, and free tiers run out.

So I built **EchoNote** — a local-first recorder that transcribes speech in real time, completely offline, with no API keys.

- **Repository:** https://github.com/Qsianuo/echonote
- **License:** MIT
- **Stack:** FastAPI · faster-whisper · WebSocket · vanilla JavaScript

---

## What it does

Click the microphone and start talking. Gray "partial" captions appear while you speak. When you pause, they turn into final, timestamped text.

You can export:

- **WAV** — the original 16 kHz recording
- **TXT** — timestamped plain text
- **Markdown** — ready for your notes or blog
- **SRT** — subtitles for editors

Everything runs on `127.0.0.1`. Audio never leaves your computer.

---

## Why local Whisper + a web UI?

### Why not a cloud API?

- **Privacy** — meeting and interview content shouldn't be uploaded to a third party.
- **Cost** — long-term usage adds up.
- **Offline** — it should work on a plane or during an outage.

### Why faster-whisper?

Whisper is one of the best open speech recognition models, but the original PyTorch implementation is slow on laptops. **faster-whisper** reimplements inference with CTranslate2 and int8 quantization, making CPU-only real-time transcription practical.

On my i5-8300H + GTX 1050 Ti, the `small` model handles live transcription comfortably.

### Why a browser instead of Electron?

Browsers already ship excellent audio APIs: `getUserMedia`, `AudioWorklet`, and `WebSocket`. An Electron app would add hundreds of megabytes for very little benefit. A local web app is cross-platform, starts instantly, and the UI is easy to build.

---

## Architecture

```text
Browser microphone
   │  getUserMedia + AudioWorklet
   ▼
16 kHz mono PCM ──WebSocket──▶ FastAPI
                                 │
                                 ▼
                        Energy-based VAD
                        │              │
                 (mid-sentence)   (pause ≥ 650ms)
                        │              │
                        ▼              ▼
                  faster-whisper transcription
                        │              │
                        ▼              ▼
                    partial        final text
                        └──────┬───────┘
                               ▼
                         Live captions
```

The division of labor is simple: the browser captures and renders, Python segments and transcribes, and WebSocket carries raw PCM between them.

---

## The interesting parts

### 1. Capture raw PCM instead of MediaRecorder

Most tutorials record with `MediaRecorder` into WebM and send chunks to the server. But decoding WebM server-side requires ffmpeg — a heavy dependency with awkward streaming semantics.

Instead, EchoNote captures raw samples with an **AudioWorklet**:

```js
const ctx = new AudioContext({ sampleRate: 16000 });
await ctx.audioWorklet.addModule('pcm-worklet.js');

const source = ctx.createMediaStreamSource(stream);
const worklet = new AudioWorkletNode(ctx, 'pcm16');
source.connect(worklet);

worklet.port.onmessage = (event) => {
  ws.send(event.data); // Int16 PCM as a binary frame
};
```

The worklet converts Float32 to Int16 and linearly resamples to 16 kHz, so the server always receives clean, decoder-free PCM:

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
      this.port.postMessage(new Int16Array(out).buffer, []);
    }

    const keepFrom = Math.floor(this.pos);
    this.buffer = merged.slice(keepFrom);
    this.pos -= keepFrom;
    return true;
  }
}
```

### 2. Segment by pauses, not by a timer

Whisper is not a streaming model. Transcribing an entire long recording at once means waiting a long time and losing the "live" feel.

EchoNote uses **energy-based voice activity detection** to split speech at natural pauses. Each 30 ms frame is scored by RMS against an adaptive noise floor:

```python
rms = np.sqrt(np.mean(np.square(frame)))
threshold = max(0.010, noise_floor * 3.0)
is_voice = rms > threshold
```

The state machine:

- voice detected → start buffering;
- silence longer than **650 ms** → finalize the utterance;
- speech shorter than **280 ms** → discard (keyboard clicks, coughs);
- a single utterance longer than **15 s** → force a split.

While you're still speaking, the current buffer is transcribed every **1.2 s** and emitted as a partial caption. After a pause, the final text is emitted.

That's the balance: immediate feedback during speech, complete sentences after it.

### 3. Serve the model locally

The model is loaded once at startup:

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

Notes:

- `compute_type="int8"` — substantial CPU speedup with minimal accuracy loss;
- `beam_size=1` — favor latency for live use;
- `condition_on_previous_text=False` — prevents context bleed between independent segments;
- `initial_prompt` — nudges Whisper toward Simplified Chinese and punctuation; without it, it often outputs Traditional Chinese with no punctuation.

### 4. A tiny WebSocket protocol

The client and server exchange very little:

| Direction | Message | Meaning |
|-----------|---------|---------|
| C → S | binary frame | 16 kHz Int16 PCM |
| C → S | `{"type":"config","language":"zh"}` | switch language |
| C → S | `{"type":"flush"}` | finalize the current utterance |
| S → C | `{"type":"partial","text":"..."}` | live caption, replaced later |
| S → C | `{"type":"final","id":1,"text":"...","start":1.2,"end":3.8}` | committed text |

The UI keeps the partial caption pinned to the bottom and turns it into a permanent paragraph when the final message arrives.

### 5. Export without a backend

All PCM chunks are kept in the browser. Writing a WAV file is just a 44-byte header plus the samples:

```js
const total = chunks.reduce((n, c) => n + c.length, 0);
const buffer = new ArrayBuffer(44 + total * 2);
// write RIFF / WAVE / fmt / data headers...
chunks.forEach((chunk) => {
  new Int16Array(buffer, offset, chunk.length).set(chunk);
  offset += chunk.length * 2;
});
```

No server round-trip, no audio encoding dependency.

---

## Pitfalls I hit

1. **Traditional Chinese without punctuation.** Fixed with a Simplified Chinese `initial_prompt`. Also, auto language detection occasionally picks Traditional output; for Chinese-first apps, defaulting to `zh` is more predictable.

2. **Model downloads in China.** Hugging Face can be slow or unreachable. Setting `HF_ENDPOINT=https://hf-mirror.com` makes it reliable. You can also point `ECHONOTE_MODEL` at a local model directory.

3. **Segments that are too short.** A fixed 2-second window produced fragmented, inaccurate sentences. Pause-based segmentation with complete utterances improved accuracy significantly.

4. **Microphone APIs need a secure context.** `getUserMedia` only works on `localhost` or HTTPS, so the server binds to `127.0.0.1` by default.

5. **UI state bugs.** The empty state and the live transcript overlapped because a CSS `display: flex` rule overrode the `hidden` attribute. A global `[hidden] { display: none !important; }` fixed it. (Yes, really.)

---

## Try it

```bash
git clone https://github.com/Qsianuo/echonote.git
cd echonote
```

On Windows, double-click `start.bat`. On macOS/Linux, run `./run.sh`.

The first launch creates a virtual environment, installs dependencies, and downloads the model. Then open:

```
http://127.0.0.1:8000
```

Model recommendations:

| Model | Size | Chinese quality | Best for |
|-------|------|-----------------|----------|
| tiny | 75 MB | okay | low-end machines |
| base | 145 MB | usable | typical laptops |
| **small** | 490 MB | **good** | **recommended** |
| medium | 1.5 GB | very good | high-end machines |

---

## What's next

- [ ] System audio / dual-track recording
- [ ] Speaker diarization
- [ ] Local history and search
- [ ] Hot-swapping models
- [ ] Desktop packaging with Tauri

---

## Final thoughts

The combination of a browser frontend and a local Python backend feels underrated. You get a polished UI without Electron's weight, and powerful local AI without cloud costs or privacy trade-offs.

If you're building anything audio-related — or just want an offline transcription tool — give it a try:

**https://github.com/Qsianuo/echonote**

A star is hugely appreciated ⭐, and I'd love to hear your thoughts on segmentation strategies, model choices, or Chinese speech recognition in the Issues.

---

**Tags:** python, ai, opensource, webdev
