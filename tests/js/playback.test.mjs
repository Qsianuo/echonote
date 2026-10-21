import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const P = require('../../web/playback.js');

test('first start is currentTime + lead', () => {
  const s = P.PlaybackScheduler.create();
  assert.equal(s.nextStartAt(10), 10.02);
});

test('second start lands exactly on the first end', () => {
  const s = P.PlaybackScheduler.create();
  const at = s.nextStartAt(10);
  s.advance(at, 3);
  assert.equal(s.nextStartAt(11), 13.02);   // 不重新加 lead
});

test('starvation re-syncs to now, not to the stale queue end', () => {
  const s = P.PlaybackScheduler.create();
  s.advance(s.nextStartAt(10), 1);          // 排到 11.02
  assert.equal(s.nextStartAt(20), 20.02);   // 卡了 9 秒，必须跟当前时间走
});

test('reset rebases the timeline', () => {
  const s = P.PlaybackScheduler.create();
  s.advance(s.nextStartAt(10), 5);
  s.reset(30);
  assert.equal(s.nextTime, 30);
  assert.equal(s.nextStartAt(30), 30.02);
});

test('windowFor maps seconds to byte offsets', () => {
  const w = P.windowFor({ positionS: 100, windowS: 30, sampleRate: 16000, totalBytes: 8000000 });
  assert.equal(w.fromByte, 3200000);   // 100 * 16000 * 2
  assert.equal(w.toByte, 4160000);     // (100+30) * 16000 * 2
});

test('windowFor clamps at the end of the recording', () => {
  const w = P.windowFor({ positionS: 240, windowS: 30, sampleRate: 16000, totalBytes: 8000000 });
  assert.equal(w.toByte, 8000000);     // 不越界
  assert.ok(w.fromByte < w.toByte);
});

test('windowFor clamps negative positions to zero', () => {
  const w = P.windowFor({ positionS: -5, windowS: 30, sampleRate: 16000, totalBytes: 8000000 });
  assert.equal(w.fromByte, 0);
  assert.equal(w.toByte, 960000);
});

test('windowFor always returns Int16-aligned offsets', () => {
  const w = P.windowFor({ positionS: 1.00005, windowS: 30, sampleRate: 16000, totalBytes: 8000000 });
  assert.equal(w.fromByte % 2, 0);
  assert.equal(w.toByte % 2, 0);
});

test('windowFor clamps past the end to an empty window', () => {
  const w = P.windowFor({ positionS: 260, windowS: 30, sampleRate: 16000, totalBytes: 8000000 });
  assert.equal(w.fromByte, 8000000);
  assert.equal(w.toByte, 8000000);
});

test('windowFor keeps offsets aligned when totalBytes is odd', () => {
  const w = P.windowFor({ positionS: 249, windowS: 30, sampleRate: 16000, totalBytes: 8000001 });
  assert.equal(w.fromByte % 2, 0);
  assert.equal(w.toByte % 2, 0);
  assert.ok(w.toByte <= 8000001);
});
