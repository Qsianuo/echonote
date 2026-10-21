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
