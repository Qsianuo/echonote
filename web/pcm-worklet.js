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
const MAX_GAIN = 32; // +30 dB: for a microphone that barely registers a voice
const CEILING = 0.95; // hard stop before the Int16 conversion wraps around
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

    let blockPeak = 0;
    for (let i = 0; i < channel.length; i++) {
      const v = channel[i] < 0 ? -channel[i] : channel[i];
      if (v > blockPeak) blockPeak = v;
    }
    this.updateGain(blockPeak);

    // Append the new native-rate samples to the carry buffer.
    const merged = new Float32Array(this.buffer.length + channel.length);
    merged.set(this.buffer, 0);
    merged.set(channel, this.buffer.length);

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
