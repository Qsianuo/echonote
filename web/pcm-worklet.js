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
 *   amplify only, never attenuate; at most +12 dB; target peak -6 dBFS.
 *
 * Silence is left untouched. Boosting a block that holds nothing but the
 * microphone's own hiss is how a recorder hands the recogniser a loud hiss,
 * and the recogniser starts inventing words.
 */
const TARGET_PEAK = 0.5; // -6 dBFS: loud enough to hear, far from clipping
const MAX_GAIN = 4; // +12 dB: past this the hiss wins more than the voice
const CEILING = 0.95; // hard stop before the Int16 conversion wraps around
// -45 dBFS. Below this a block is room noise, not a voice: a hiss at -50 dBFS
// is peak 0.003, quiet speech is nearer 0.05. Amplifying noise here would lift
// the floor above the VAD's 0.010 RMS gate, the segmenter would start treating
// every pause as speech, and the decoder would run non-stop on nothing.
const NO_GAIN_PEAK = 0.0056;
const RISE = 0.12; // per native block; ~30 ms to settle at a quieter level
const FALL = 1.0; // instant: one transient must not push the next block over
const RELAX = 0.05; // per block back to unity while nothing is being said

class PCM16Processor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.targetRate = 16000;
    this.ratio = sampleRate / this.targetRate;
    this.buffer = new Float32Array(0);
    this.pos = 0;
    this.gain = 1;
  }

  /** Move the gain toward what this block needs, asymmetric on purpose. */
  updateGain(blockPeak) {
    if (blockPeak > NO_GAIN_PEAK) {
      const wanted = Math.min(MAX_GAIN, Math.max(1, TARGET_PEAK / blockPeak));
      const k = wanted < this.gain ? FALL : RISE;
      this.gain += (wanted - this.gain) * k;
    } else {
      // Nothing but room tone: slide back to unity so a pause between two
      // sentences is not recorded 12 dB louder than the room actually is.
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
