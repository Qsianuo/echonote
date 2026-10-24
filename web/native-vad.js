/**
 * EchoNote 移动端 · 能量 VAD 切句器。
 *
 * 这是 app/vad.py（服务端 Python 版）的忠实移植：同样的 30 ms 帧节拍、
 * 同样的自适应底噪与门限规则（threshold = max(base, floor × gate_ratio)）、
 * 同样的前滚环（保住轻起音）与尾保留（保住句尾软音）。字段名与分支顺序
 * 刻意与 Python 版一一对应，两边的对照测试（test/vad.test.mjs）用同一份
 * 音频样本比对切句结果，防止移植走样。
 *
 * 输入：16 kHz mono Float32（与采集 worklet 的输出一致）。
 */

export const FRAME_MS = 30;

// 与 app/config.py 的 VAD_* 默认值保持一致。
export const DEFAULTS = {
  threshold: 0.01,
  hangoverMs: 900,
  minSpeechMs: 240,
  maxSegmentS: 30,
  prerollMs: 300,
  tailKeepMs: 1000,
  gateRatio: 3.0,
};

// 录入情境预设：与 app/config.py 的 MODES 一致（个人=默认；会议=更宽的
// 底噪门限倍数 + 更长的确认/切句容忍，挡掉键盘/咳嗽这类短促杂音）。
export const MODES = {
  personal: {},
  meeting: { gateRatio: 4.0, hangoverMs: 1200, minSpeechMs: 300, prerollMs: 400 },
};

/** Python 版参数名（snake_case）→ 本类字段名（camelCase）。两边共用一套
 * 夹具（fixtures.json 由 Python 生成），构造器必须同时认两种键名，
 * 否则覆盖参数会被静默丢弃——首次对照测试就是这么黄的。 */
function normalizeOverrides(overrides = {}) {
  const map = {
    gate_ratio: 'gateRatio',
    hangover_ms: 'hangoverMs',
    min_speech_ms: 'minSpeechMs',
    max_segment_s: 'maxSegmentS',
    preroll_ms: 'prerollMs',
    tail_keep_ms: 'tailKeepMs',
  };
  const out = {};
  for (const [key, value] of Object.entries(overrides)) {
    out[map[key] || key] = value;
  }
  return out;
}

export class Segmenter {
  constructor(overrides = {}) {
    const cfg = { ...DEFAULTS, ...normalizeOverrides(overrides) };
    this.sampleRate = 16000;
    this.frameSize = Math.floor((this.sampleRate * FRAME_MS) / 1000); // 480
    this.baseThreshold = cfg.threshold;
    // 帧须高出「实测底噪」多少倍才算语音；会议模式抬高这一倍数而不是
    // 绝对门限——远场人声更轻，抬高绝对门限会把要听的人先挡掉。
    this.gateRatio = cfg.gateRatio;
    this.hangoverFrames = Math.max(1, Math.round(cfg.hangoverMs / FRAME_MS));
    this.minSpeechFrames = Math.max(1, Math.round(cfg.minSpeechMs / FRAME_MS));
    this.maxSegmentFrames = Math.max(1, Math.round((cfg.maxSegmentS * 1000) / FRAME_MS));
    this.tailKeepFrames = Math.max(1, Math.round(cfg.tailKeepMs / FRAME_MS));
    this.prerollFrames = Math.max(0, Math.floor(cfg.prerollMs / FRAME_MS));
    this._ring = []; // 未开口时的前滚帧，开口后拼回句首

    this._pending = new Float32Array(0);
    this.totalFrames = 0;
    this.noiseFloor = 0.004;

    this.inSpeech = false;
    this.frames = [];
    this.speechFrames = 0;
    this.silenceRun = 0;
    this.segmentStartFrame = 0;
    this.lastSpeechFrame = 0;

    this.lastRms = 0;
    this.lastThreshold = this.baseThreshold;
    this.segmentPeakRms = 0;
  }

  /** 喂入音频，返回本轮完成的段落（与 push() 语义一致）。 */
  push(samples) {
    if (!samples || samples.length === 0) return [];
    const merged = new Float32Array(this._pending.length + samples.length);
    merged.set(this._pending, 0);
    merged.set(samples, this._pending.length);
    this._pending = merged;

    const events = [];
    while (this._pending.length >= this.frameSize) {
      const frame = this._pending.subarray(0, this.frameSize);
      const event = this._processFrame(frame);
      if (event) events.push(event);
      this._pending = this._pending.slice(this.frameSize);
    }
    return events;
  }

  /** 进行中段落的首帧预览（实时字幕用）。 */
  currentAudio() {
    if (!this.inSpeech || this.frames.length === 0) return null;
    let keep = this.lastSpeechFrame - this.segmentStartFrame + 1;
    keep = Math.max(1, Math.min(keep + this.tailKeepFrames, this.frames.length));
    const audio = concatFloats(this.frames.slice(0, keep));
    if (audio.length < this.sampleRate * 0.25) return null;
    return audio;
  }

  get segmentStartS() {
    return (this.segmentStartFrame * FRAME_MS) / 1000;
  }

  flush() {
    if (!this.inSpeech) return null;
    return this._finalize();
  }

  _processFrame(frame) {
    let sum = 0;
    for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
    const rms = Math.sqrt(sum / frame.length);
    const threshold = Math.max(this.baseThreshold, this.noiseFloor * this.gateRatio);
    const isVoice = rms > threshold;
    this.lastRms = rms;
    this.lastThreshold = threshold;
    if (rms > this.segmentPeakRms) this.segmentPeakRms = rms;

    // 只有在「明确不是语音」时才更新底噪，与 Python 版一致。
    if (rms < Math.max(this.baseThreshold * 0.6, this.noiseFloor * 1.5)) {
      this.noiseFloor = 0.9 * this.noiseFloor + 0.1 * rms;
    } else if (!this.inSpeech && rms < threshold) {
      this.noiseFloor = 0.995 * this.noiseFloor + 0.005 * rms;
    }

    let event = null;

    if (isVoice) {
      if (!this.inSpeech) {
        this.inSpeech = true;
        this.frames = [];
        this.speechFrames = 0;
        this.silenceRun = 0;
        this.segmentStartFrame = this.totalFrames;
        const nPre = this._ring.length;
        if (nPre > 0) {
          // 前滚帧成为句首，时间戳要一起前移，否则导出的 SRT 与音频错位。
          this.frames = this._ring.slice();
          this.segmentStartFrame = Math.max(0, this.segmentStartFrame - nPre);
        }
        this._ring = [];
      }
      this.silenceRun = 0;
      this.speechFrames += 1;
      this.frames.push(copyOf(frame));
      this.lastSpeechFrame = this.totalFrames;

      if (this.frames.length >= this.maxSegmentFrames) event = this._finalize();
    } else if (this.inSpeech) {
      this.frames.push(copyOf(frame));
      this.silenceRun += 1;
      if (this.silenceRun >= this.hangoverFrames) event = this._finalize();
    } else if (this.prerollFrames > 0) {
      this._ring.push(copyOf(frame));
      if (this._ring.length > this.prerollFrames) this._ring.shift();
    }

    this.totalFrames += 1;
    return event;
  }

  _finalize() {
    if (this.frames.length === 0) {
      this._resetSegment();
      return null;
    }

    // 尾部保留：逐帧（而非整段均值）判断尾部是否纯静音——一句 300 ms 的
    // 软尾跟 700 ms 真静音的均值会低于门限，按均值裁就会吃掉句尾。
    let keep = this.frames.length;
    if (keep > this.tailKeepFrames) {
      const tail = concatFloats(this.frames.slice(-this.tailKeepFrames));
      const silenceFloor = this.baseThreshold * 0.15;
      let tailHasSignal = false;
      for (let i = 0; i < tail.length; i++) {
        if (Math.abs(tail[i]) > silenceFloor) {
          tailHasSignal = true;
          break;
        }
      }
      if (!tailHasSignal) {
        keep = Math.min(
          keep,
          this.lastSpeechFrame - this.segmentStartFrame + 1 + this.tailKeepFrames
        );
      }
    }
    const audio = keep > 0 ? concatFloats(this.frames.slice(0, keep)) : null;

    const start = (this.segmentStartFrame * FRAME_MS) / 1000;
    // end 取最后一个有声帧：SRT 与「句子结束判断」要的是停声时刻，
    // 不是缓冲区交出的时刻。
    const end = ((this.lastSpeechFrame + 1) * FRAME_MS) / 1000;
    const speechFrames = this.speechFrames;
    const speechOk = speechFrames >= this.minSpeechFrames;

    this._resetSegment();

    if (!speechOk || !audio || audio.length < this.sampleRate * 0.25) return null;
    return { type: 'final', audio, start, end };
  }

  _resetSegment() {
    this.inSpeech = false;
    this.frames = [];
    this.speechFrames = 0;
    this.silenceRun = 0;
    this.lastSpeechFrame = this.segmentStartFrame;
    this.segmentPeakRms = 0;
  }
}

function concatFloats(list) {
  let total = 0;
  for (const item of list) total += item.length;
  const out = new Float32Array(total);
  let offset = 0;
  for (const item of list) {
    out.set(item, offset);
    offset += item.length;
  }
  return out;
}

function copyOf(view) {
  return new Float32Array(view);
}
