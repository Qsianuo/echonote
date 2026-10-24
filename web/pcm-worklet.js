/**
 * EchoNote PCM worklet
 * Converts microphone float samples to 16 kHz mono Int16 PCM and posts
 * them to the main thread as transferable ArrayBuffers.
 *
 * Level control lives here rather than in getUserMedia's ``autoGainControl``.
 * That flag is tuned for phone calls: it attenuates as readily as it boosts,
 * so a recording made next to a laptop fan comes out quiet again, and it is
 * invisible — there is no way to ask "why is this file so quiet" and get an
 * answer. The rule below is one-directional and bounded on purpose:
 *
 *   amplify only, never attenuate; at most +24 dB; target peak -3 dBFS.
 *
 * What counts as "worth amplifying" is measured against this machine's own
 * noise floor, not a fixed level. An absolute gate either lets a quiet
 * microphone through untouched (the file stays quiet, which defeats the
 * point) or lifts the room hiss along with the voice (the VAD then reads
 * every pause as speech and the decoder runs non-stop on nothing). Tracking
 * the floor block by block separates the two cases on the only evidence
 * available: whether a block stands above what the room is already doing.
 */
const TARGET_PEAK = 0.8; // -1.9 dBFS: only the ceiling is left above it
const MAX_GAIN = 16; // +24 dB: for a microphone that barely registers a voice
const CEILING = 0.95; // hard stop before the Int16 conversion wraps around
// High-pass corner. Desk bumps, HVAC rumble, mic-wire handling noise and the
// transformer hum all live below ~80 Hz and carry no speech — but they inflate
// the block peak and the tracked noise floor, which is how a truck outside
// reads as continuous voice and gets amplified along with everything else.
// 80 Hz sits above that rumble and below the F0 of even the deepest voices.
const HPF_HZ = 80;
const HPF_Q = 0.7071; // Butterworth: maximally flat passband, -12 dB/octave
// 工频陷波（50/100/150 Hz，Q=30 窄带）。手机插着充电线录音时，USB 供电
// 噪声会经地线耦合进麦克风（两台测试机都有"电流声"的直接嫌疑），基波与
// 前两个谐波正好落在这里。窄带陷波只挖掉这三条谱线，不碰语音频段。
// 串在增益/底噪估计之前：不让嗡嗡污染 floor，也不给它吃 AGC 增益。
const NOTCHES = [50, 100, 150];
const NOTCH_Q = 30;
// 抗混叠低通（7.5 kHz，Butterworth）。降采样 44.1k/48k→16k 用的是线性
// 插值，没有它的话奈奎斯特以上的全部能量（10k-24k）会折叠进 0-8kHz
// 人声频段——表现就是录音里那层金属感的"电流"伪影。
const AA_HZ = 7500;
const AA_Q = 0.7071;
// Absolute floor. Even in a silent room the converter dithers around -66 dBFS;
// below that there is nothing to scale and the gain stays at unity.
const NOISE_ABS = 0.0005;
const FLOOR_RISE = 0.001; // slow creep up, so one utterance cannot hide the floor
const FLOOR_FALL = 0.2; // quick drop, so a pause re-establishes it at once
const SPEECH_MARGIN = 2.5; // ~8 dB above the floor before a block counts as voice
const RISE = 0.12; // per native block; ~30 ms to settle at a quieter level
const FALL = 1.0; // instant: one transient must not push the next block over
const RELAX = 0.08; // per block back to unity while nothing is being said

class PCM16Processor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.targetRate = 16000;
    this.ratio = sampleRate / this.targetRate;
    this.buffer = new Float32Array(0);
    this.pos = 0;
    this.gain = 1;
    this.floor = 0; // running estimate of the room's own level
    this.seeded = false;

    // 滤波链系数（RBJ cookbook），每个双二阶级一对状态。链路顺序：
    // 工频陷波 ×3 → 高通 → 抗混叠低通，全部先于增益与底噪估计。
    const mkBiquad = (type, f0, q) => {
      const w0 = (2 * Math.PI * f0) / sampleRate;
      const cosW0 = Math.cos(w0);
      const alpha = Math.sin(w0) / (2 * q);
      let b0, b1, b2, a0, a1, a2;
      if (type === 'notch') {
        b0 = 1 / (1 + alpha); b1 = (-2 * cosW0) / (1 + alpha);
        b2 = 1 / (1 + alpha); a0 = 1;
        a1 = (-2 * cosW0) / (1 + alpha); a2 = (1 - alpha) / (1 + alpha);
      } else {
        // highpass
        a0 = 1 + alpha;
        b0 = (1 + cosW0) / 2 / a0;
        b1 = (-(1 + cosW0)) / a0;
        b2 = (1 + cosW0) / 2 / a0;
        a1 = (-2 * cosW0) / a0;
        a2 = (1 - alpha) / a0;
      }
      return { b0, b1, b2, a1, a2, x1: 0, x2: 0, y1: 0, y2: 0 };
    };
    this.chain = [
      ...NOTCHES.map((f) => mkBiquad('notch', f, NOTCH_Q)),
      mkBiquad('highpass', HPF_HZ, HPF_Q),
      // 两级级联（共 24 dB/oct）：单级在 12 kHz 只有 -11 dB，混叠仍可闻；
      // 两级后 10 kHz 以上压到 -18 dB 以下，代价是 6-8 kHz 轻微变闷（语音
      // 可懂度几乎不受影响）。
      mkBiquad('lowpass', AA_HZ, AA_Q),
      mkBiquad('lowpass', AA_HZ, AA_Q),
    ];
  }

  /** Move the gain toward what this block needs, asymmetric on purpose. */
  updateGain(blockPeak) {
    if (!this.seeded) {
      this.floor = blockPeak;
      this.seeded = true;
    } else if (blockPeak < this.floor) {
      this.floor += (blockPeak - this.floor) * FLOOR_FALL;
    } else {
      this.floor += (blockPeak - this.floor) * FLOOR_RISE;
    }

    if (blockPeak > NOISE_ABS && blockPeak > this.floor * SPEECH_MARGIN) {
      const wanted = Math.min(MAX_GAIN, Math.max(1, TARGET_PEAK / blockPeak));
      const k = wanted < this.gain ? FALL : RISE;
      this.gain += (wanted - this.gain) * k;
    } else {
      // Room tone, not a voice: slide back to unity so a pause between two
      // sentences is not recorded 24 dB louder than the room actually is.
      this.gain += (1 - this.gain) * RELAX;
    }
    if (!(this.gain >= 1)) this.gain = 1; // NaN guard, and never attenuate
    if (this.gain > MAX_GAIN) this.gain = MAX_GAIN;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;

    const channel = input[0];
    if (!channel || channel.length === 0) return true;

    // 滤波链先行：陷波掉工频、高通掉隆隆、低通防混叠——之后底噪估计、
    // 增益判断和降采样看到的才是干净的语音频带。
    const filtered = new Float32Array(channel.length);
    for (let i = 0; i < channel.length; i++) {
      let x = channel[i];
      for (let s = 0; s < this.chain.length; s++) {
        const f = this.chain[s];
        const y =
          f.b0 * x + f.b1 * f.x1 + f.b2 * f.x2 - f.a1 * f.y1 - f.a2 * f.y2;
        f.x2 = f.x1;
        f.x1 = x;
        f.y2 = f.y1;
        f.y1 = y;
        x = y;
      }
      filtered[i] = x;
    }

    let blockPeak = 0;
    for (let i = 0; i < filtered.length; i++) {
      const v = filtered[i] < 0 ? -filtered[i] : filtered[i];
      if (v > blockPeak) blockPeak = v;
    }
    this.updateGain(blockPeak);

    // Append the new native-rate samples to the carry buffer.
    const merged = new Float32Array(this.buffer.length + filtered.length);
    merged.set(this.buffer, 0);
    merged.set(filtered, this.buffer.length);

    const maxOut = Math.floor(Math.max(0, merged.length - 1 - this.pos) / this.ratio) + 1;
    if (maxOut <= 0) {
      this.buffer = merged;
      return true;
    }

    const out = new Int16Array(maxOut);
    const gain = this.gain;
    let written = 0;

    while (this.pos + 1 < merged.length) {
      const index = Math.floor(this.pos);
      const frac = this.pos - index;
      const s0 = merged[index];
      const s1 = merged[index + 1];
      let sample = (s0 + (s1 - s0) * frac) * gain;

      if (sample > CEILING) sample = CEILING;
      else if (sample < -CEILING) sample = -CEILING;

      out[written++] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      this.pos += this.ratio;
    }

    if (written > 0) {
      this.port.postMessage(out.buffer, [out.buffer]);
    }

    // Keep the unconsumed tail, preserving the fractional position.
    const keepFrom = Math.floor(this.pos);
    this.buffer = merged.slice(keepFrom);
    this.pos -= keepFrom;

    return true;
  }
}

registerProcessor('pcm16', PCM16Processor);
