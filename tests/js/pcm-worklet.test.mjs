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

function sine(amplitude, seconds = 0.6, rate = 16000, hz = 220) {
  const n = Math.round(rate * seconds);
  const block = new Float32Array(128);
  const blocks = [];
  for (let start = 0; start < n; start += 128) {
    for (let i = 0; i < 128; i += 1) {
      const t = (start + i) / rate;
      block[i] = amplitude * Math.sin(2 * Math.PI * hz * t);
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

const FULL = 0x7fff;

test('quiet speech is lifted to the -6 dBFS target', () => {
  // 0.2 peak needs 2.5x to reach the 0.5 target, which is inside the cap.
  const Processor = loadWorklet();
  const out = run(new Processor(), sine(0.2));
  const peak = peakOf(out) / FULL;
  assert.ok(peak > 0.42 && peak < 0.56, `expected ~0.5, got ${peak.toFixed(3)}`);
});

test('very quiet input stops at the +12 dB cap instead of amplifying forever', () => {
  const Processor = loadWorklet();
  const out = run(new Processor(), sine(0.03));
  const peak = peakOf(out) / FULL;
  // 0.03 * 4 = 0.12; anything higher means the cap leaked.
  assert.ok(peak > 0.10 && peak < 0.135, `expected ~0.12, got ${peak.toFixed(3)}`);
});

test('a healthy loud recording is never attenuated', () => {
  const Processor = loadWorklet();
  const out = run(new Processor(), sine(0.9));
  const peak = peakOf(out) / FULL;
  assert.ok(peak > 0.85, `loud input must not be pulled down, got ${peak.toFixed(3)}`);
});

test('output never exceeds the ceiling, so Int16 cannot wrap around', () => {
  const Processor = loadWorklet();
  const out = run(new Processor(), sine(1.0));
  const peak = peakOf(out) / FULL;
  assert.ok(peak <= 0.96, `ceiling breached: ${peak.toFixed(3)}`);
  for (let i = 0; i < out.length; i += 1) {
    assert.ok(out[i] > -0x8000 && out[i] < 0x8000);
  }
});

test('silence stays silent: hiss is not amplified into words', () => {
  const Processor = loadWorklet();
  const silence = Array.from({ length: 20 }, () => new Float32Array(128));
  const out = run(new Processor(), silence);
  assert.equal(peakOf(out), 0);
});

test('gain never drops below unity, even right after a loud transient', () => {
  const Processor = loadWorklet();
  const processor = new Processor();
  run(processor, sine(1.0, 0.1)); // slam the limiter
  run(processor, sine(0.02, 0.1)); // then go quiet
  assert.ok(processor.gain >= 1 && processor.gain <= 4, `gain out of range: ${processor.gain}`);
});
