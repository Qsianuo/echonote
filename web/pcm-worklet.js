/**
 * EchoNote PCM worklet
 * Converts microphone float samples to 16 kHz mono Int16 PCM and posts
 * them to the main thread as transferable ArrayBuffers.
 */
class PCM16Processor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.targetRate = 16000;
    this.ratio = sampleRate / this.targetRate;
    this.buffer = new Float32Array(0);
    this.pos = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;

    const channel = input[0];
    if (!channel || channel.length === 0) return true;

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
    let written = 0;

    while (this.pos + 1 < merged.length) {
      const index = Math.floor(this.pos);
      const frac = this.pos - index;
      const s0 = merged[index];
      const s1 = merged[index + 1];
      let sample = s0 + (s1 - s0) * frac;

      if (sample > 1) sample = 1;
      else if (sample < -1) sample = -1;

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
