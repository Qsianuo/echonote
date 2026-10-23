import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const SOURCE = readFileSync(join(HERE, '../../web/pcm-worklet.js'), 'utf8');

/** Load the real worklet source with the AudioWorklet globals stubbed out. */
function loadWorklet(rate = 16000) {
  let registered = null;
  const context = {
    sampleRate: rate,
    AudioWorkletProcessor: class {
      constructor() {
        this.port = { postMessage() {} };
      }
    },
    registerProcessor: (name, cls) => {
      assert.equal(name, 'pcm16');
      registered = cls;
    },
  };
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(SOURCE, context);
  assert.ok(registered, 'worklet did not register a processor');
  return registered;
}

/** Feed blocks, collect the Int16 the processor would have posted. */
function run(processor, blocks) {
  const chunks = [];
  processor.port.postMessage = (buffer) => chunks.push(new Int16Array(buffer));
  for (const block of blocks) processor.process([[block]]);
  const total = chunks.reduce((sum, c) => sum + c.length, 0);
  const out = new Int16Array(total);
  let at = 0;
  for (const chunk of chunks) {
    out.set(chunk, at);
    at += chunk.length;
  }
  return out;
}

const RATE = 16000;

/**
 * Speech is not a constant tone: it is bursts separated by pauses. The floor
 * tracker relies on that shape, so the fixtures have it too — a steady sine
 * would legitimately look like room tone and never get amplified.
 */
function utterance({ speechPeak, gapPeak = 0.001, speechSeconds = 0.4, gapSeconds = 0.3, cycles = 3 }) {
  const blocks = [];
  // Every real recording opens on a moment of room tone; the floor tracker
  // needs that to bootstrap, otherwise it seeds on the first syllable.
  blocks.push(...tone(gapPeak, gapSeconds, 4000));
  for (let cycle = 0; cycle < cycles; cycle += 1) {
    blocks.push(...tone(speechPeak, speechSeconds, 220));
    blocks.push(...tone(gapPeak, gapSeconds, 4000));
  }
  return blocks;
}

function tone(amplitude, seconds, hz) {
  const rate = RATE;
  const n = Math.round(rate * seconds);
  const blocks = [];
  const block = new Float32Array(128);
  for (let start = 0; start < n; start += 128) {
    for (let i = 0; i < 128; i += 1) {
      block[i] = amplitude * Math.sin((2 * Math.PI * hz * (start + i)) / rate);
    }
    blocks.push(Float32Array.from(block));
  }
  return blocks;
}

function peakOf(int16) {
  let peak = 0;
  for (let i = 0; i < int16.length; i += 1) {
    const v = int16[i] < 0 ? -int16[i] : int16[i];
    if (v > peak) peak = v;
  }
  return peak;
}

/** Peak of the loudest block, so a quiet gap cannot mask the speech level. */
function speechPeakOf(int16) {
  let best = 0;
  for (let i = 0; i + 128 <= int16.length; i += 128) {
    let peak = 0;
    for (let j = 0; j < 128; j += 1) {
      const v = int16[i + j] < 0 ? -int16[i + j] : int16[i + j];
      if (v > peak) peak = v;
    }
    if (peak > best) best = peak;
  }
  return best;
}

const FULL = 0x7fff;
const db = (x) => 20 * Math.log10(Math.max(x, 1e-9));

test('a quiet voice standing above the floor is lifted toward -3 dBFS', () => {
  // Room at -60 dBFS, voice at -34 dBFS: exactly the "too quiet" complaint.
  const Processor = loadWorklet();
  const out = run(new Processor(), utterance({ speechPeak: 0.02, gapPeak: 0.001 }));
  const peak = speechPeakOf(out) / FULL;
  assert.ok(peak > 0.25, `expected a large lift, got ${db(peak).toFixed(1)} dBFS`);
  assert.ok(peak <= 0.96, 'must stay under the ceiling');
});

test('a very quiet low-gain microphone still reaches a usable level', () => {
  // -46 dBFS voice: unusable as recorded, and the old fixed gate blocked it.
  const Processor = loadWorklet();
  const out = run(new Processor(), utterance({ speechPeak: 0.005, gapPeak: 0.0002 }));
  const peak = speechPeakOf(out) / FULL;
  assert.ok(peak > 0.05, `expected the +24 dB cap to apply, got ${db(peak).toFixed(1)} dBFS`);
});

test('steady room tone is never amplified', () => {
  // No pauses means no voice: multiplying this is what made the VAD fire on
  // every silence and left the decoder running non-stop on nothing.
  const Processor = loadWorklet();
  const out = run(new Processor(), tone(0.003, 1.5, 4000));
  const peak = peakOf(out) / FULL;
  assert.ok(peak < 0.0035, `room tone was amplified to ${db(peak).toFixed(1)} dBFS`);
});

test('a healthy loud recording is never attenuated', () => {
  const Processor = loadWorklet();
  const out = run(new Processor(), utterance({ speechPeak: 0.9, gapPeak: 0.001 }));
  const peak = speechPeakOf(out) / FULL;
  assert.ok(peak > 0.85, `loud input must not be pulled down, got ${peak.toFixed(3)}`);
});

test('output never exceeds the ceiling, so Int16 cannot wrap around', () => {
  const Processor = loadWorklet();
  const out = run(new Processor(), utterance({ speechPeak: 1.0, gapPeak: 0.05 }));
  assert.ok(peakOf(out) / FULL <= 0.96, 'ceiling breached');
  for (let i = 0; i < out.length; i += 1) {
    assert.ok(out[i] > -0x8000 && out[i] < 0x8000);
  }
});

test('silence stays silent', () => {
  const Processor = loadWorklet();
  const silence = Array.from({ length: 20 }, () => new Float32Array(128));
  assert.equal(peakOf(run(new Processor(), silence)), 0);
});

test('gain relaxes back to unity during a long pause', () => {
  const Processor = loadWorklet();
  const processor = new Processor();
  run(processor, tone(0.001, 0.3, 4000)); // room tone seeds the floor
  run(processor, tone(0.02, 0.4, 220)); // quiet speech lifts the gain
  const lifted = processor.gain;
  assert.ok(lifted > 4, `gain should have risen, got ${lifted.toFixed(2)}`);
  run(processor, tone(0.001, 1.0, 4000)); // long pause
  assert.ok(processor.gain < 1.3, `gain should relax, got ${processor.gain.toFixed(2)}`);
});

test('gain never leaves the [1, 16] range', () => {
  const Processor = loadWorklet();
  const processor = new Processor();
  run(processor, utterance({ speechPeak: 1.0, gapPeak: 0.001 }));
  run(processor, utterance({ speechPeak: 0.0001, gapPeak: 0.00001 }));
  assert.ok(processor.gain >= 1 && processor.gain <= 16, `gain out of range: ${processor.gain}`);
});
