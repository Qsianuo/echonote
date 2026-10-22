import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const Store = require('../../web/audio-store.js');

test('starts empty', () => {
  const l = Store.PcmLedger.create({ maxBytes: 1000 });
  assert.equal(l.byteLength, 0);
  assert.equal(l.sampleCount, 0);
  assert.equal(l.overBudget, false);
});

test('accept accumulates bytes as samples * 2', () => {
  const l = Store.PcmLedger.create({ maxBytes: 1000 });
  l.accept(new Int16Array(100));
  assert.equal(l.byteLength, 200);
  assert.equal(l.sampleCount, 100);
});

test('accepting past the budget still records every byte (永不截断录音)', () => {
  const l = Store.PcmLedger.create({ maxBytes: 300 });
  l.accept(new Int16Array(100));   // 200 B
  l.accept(new Int16Array(100));   // 400 B，超出预算但必须照收
  assert.equal(l.byteLength, 400);
  assert.equal(l.overBudget, true);
});

test('overBudget flips exactly at the limit, not before', () => {
  const l = Store.PcmLedger.create({ maxBytes: 200 });
  l.accept(new Int16Array(100));   // 恰好 200 B
  assert.equal(l.overBudget, false);
  l.accept(new Int16Array(1));     // 202 B
  assert.equal(l.overBudget, true);
});

test('reset clears accounting', () => {
  const l = Store.PcmLedger.create({ maxBytes: 1000 });
  l.accept(new Int16Array(10));
  l.reset();
  assert.equal(l.byteLength, 0);
  assert.equal(l.overBudget, false);
});

test('rejects non-Int16Array input', () => {
  const l = Store.PcmLedger.create({ maxBytes: 1000 });
  assert.throws(() => l.accept(new Float32Array(4)), TypeError);
  assert.throws(() => l.accept(null), TypeError);
});

test('empty chunk is accepted and changes nothing', () => {
  const l = Store.PcmLedger.create({ maxBytes: 1000 });
  l.accept(new Int16Array(0));
  assert.equal(l.byteLength, 0);
});

test('MemoryStore accumulates bytes and samples as frames arrive', () => {
  const s = Store.createMemoryStore();
  assert.equal(s.fallback, true);
  assert.equal(s.totalBytes, 0);
  s.append(new Int16Array(100));
  s.append(new Int16Array(50));
  assert.equal(s.totalBytes, 300);
  assert.equal(s.sampleCount, 150);
  s.append(new Int16Array(0));
  assert.equal(s.totalBytes, 300);
});

test('MemoryStore stores a copy, so the caller can reuse its buffer', () => {
  const s = Store.createMemoryStore();
  const frame = new Int16Array([1, 2, 3]);
  s.append(frame);
  frame[0] = 99;
  return s.read(0, 6).then((buffer) => {
    assert.deepEqual(Array.from(new Int16Array(buffer)), [1, 2, 3]);
  });
});

test('MemoryStore read returns exactly the requested byte range', async () => {
  const s = Store.createMemoryStore();
  s.append(Int16Array.from([1, 2, 3, 4, 5, 6]));
  assert.deepEqual(Array.from(new Int16Array(await s.read(0, 12))), [1, 2, 3, 4, 5, 6]);
  assert.deepEqual(Array.from(new Int16Array(await s.read(8, 12))), [5, 6]);
  assert.equal((await s.read(0, 2)).byteLength, 2);
});

test('MemoryStore toBlob reports the PCM byte length', async () => {
  const s = Store.createMemoryStore();
  s.append(new Int16Array(1600)); // 3200 B
  s.append(new Int16Array(400));  // 800 B
  assert.equal((await s.toBlob()).size, 4000);
});

test('MemoryStore discard clears the audio and refuses further reads', async () => {
  const s = Store.createMemoryStore();
  s.append(new Int16Array(1600));
  await s.discard();
  assert.equal(s.totalBytes, 0);
  await assert.rejects(() => s.read(0, 3200), /已清空/);
  await assert.rejects(() => s.toBlob(), /已清空/);
});
