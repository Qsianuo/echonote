import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const Wav = require('../../web/wav.js');

const RATE = 16000;

function chunk(values) {
  return Int16Array.from(values);
}

function oneSecond() {
  const samples = new Int16Array(RATE);
  for (let index = 0; index < RATE; index += 1) samples[index] = index % 1000;
  return samples;
}

function ascii(view, offset, length) {
  let out = '';
  for (let index = 0; index < length; index += 1) {
    out += String.fromCharCode(view.getUint8(offset + index));
  }
  return out;
}

test('WAV header is PCM / mono / 16 kHz / 16 bit', () => {
  const view = new DataView(Wav.buildWavBuffer([oneSecond()], RATE));

  assert.equal(ascii(view, 0, 4), 'RIFF');
  assert.equal(ascii(view, 8, 4), 'WAVE');
  assert.equal(ascii(view, 12, 4), 'fmt ');
  assert.equal(view.getUint32(16, true), 16);
  assert.equal(view.getUint16(20, true), 1); // PCM
  assert.equal(view.getUint16(22, true), 1); // mono
  assert.equal(view.getUint32(24, true), 16000);
  assert.equal(view.getUint32(28, true), 32000);
  assert.equal(view.getUint16(32, true), 2);
  assert.equal(view.getUint16(34, true), 16);
  assert.equal(ascii(view, 36, 4), 'data');
});

test('WAV sizes match the sample count', () => {
  const samples = oneSecond();
  const buffer = Wav.buildWavBuffer([samples], RATE);
  const view = new DataView(buffer);

  assert.equal(buffer.byteLength, 44 + samples.length * 2);
  assert.equal(view.getUint32(4, true), 36 + samples.length * 2);
  assert.equal(view.getUint32(40, true), samples.length * 2);
});

test('WAV samples survive the round trip in order', () => {
  const first = chunk([1, 2, 3]);
  const second = chunk([-4, 5, -6]);
  const view = new DataView(Wav.buildWavBuffer([first, second], RATE));

  const read = [1, 2, 3, -4, 5, -6].map((_, index) => view.getInt16(44 + index * 2, true));
  assert.deepEqual(read, [1, 2, 3, -4, 5, -6]);
});

test('duration and byte length derive from the samples', () => {
  assert.equal(Wav.sampleCount([oneSecond()]), 16000);
  assert.equal(Wav.durationSeconds([oneSecond()], RATE), 1);
  assert.equal(Wav.byteLength([oneSecond()]), 32000);
});

test('empty input still produces a valid 44-byte header', () => {
  const buffer = Wav.buildWavBuffer([], RATE);
  assert.equal(buffer.byteLength, 44);
  assert.equal(new DataView(buffer).getUint32(40, true), 0);
});

test('merging 100 ms frames does not change the audio length (FR-038 AC6)', () => {
  const frames = [];
  for (let index = 0; index < 10; index += 1) frames.push(new Int16Array(1600).fill(index));
  const merged = Wav.sampleCount(frames);

  assert.equal(merged, 16000);
  assert.equal(Wav.durationSeconds(frames, RATE), 1);
});
