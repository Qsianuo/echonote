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

// read() 不再走 toBlob() 全量重建（v0.12.0 回放按 30 s 窗口读），改成逐块按字节
// 切片；这条用例锁住跨块拼接、跨块区间与空区间的语义 —— 单块用例测不到这些分支。
test('MemoryStore read stitches across chunks without rebuilding a blob', async () => {
  const s = Store.createMemoryStore();
  s.append(new Int16Array(8).fill(1)); // 16 B，样本 0-7
  s.append(new Int16Array(8).fill(2)); // 16 B，样本 8-15
  s.append(new Int16Array(8).fill(3)); // 16 B，样本 16-23
  assert.equal(s.totalBytes, 48);

  const whole = await s.read(0, 48);
  assert.equal(whole.byteLength, 48);
  assert.deepEqual(
    Array.from(new Int16Array(whole)),
    [
      ...Array(8).fill(1),
      ...Array(8).fill(2),
      ...Array(8).fill(3),
    ]
  );

  // 12..36 跨三块：第一块尾部 2 个样本 + 第二块整块 + 第三块头部 2 个样本
  const middle = await s.read(12, 36);
  assert.equal(middle.byteLength, 24);
  assert.deepEqual(
    Array.from(new Int16Array(middle)),
    [
      ...Array(2).fill(1), // 第一块最后 2 个样本
      ...Array(8).fill(2), // 第二块整块
      ...Array(2).fill(3), // 第三块前 2 个样本
    ]
  );

  assert.equal((await s.read(0, 0)).byteLength, 0);
  assert.equal((await s.read(48, 48)).byteLength, 0);
});

// I5 的另一半是「别再走全量拷贝」，这条用替换 toBlob 的方式把它钉住：
// 旧实现 read() 内部调 store.toBlob()，一跑就抛；新实现根本不碰 toBlob。
test('MemoryStore read never routes through toBlob', async () => {
  const s = Store.createMemoryStore();
  s.append(new Int16Array(1600).fill(7)); // 3200 B
  s.toBlob = () => {
    throw new Error('read 不应重建全量 Blob');
  };
  const buffer = await s.read(0, 3200);
  assert.equal(buffer.byteLength, 3200);
  const pcm = new Int16Array(buffer);
  assert.deepEqual([pcm[0], pcm[1599]], [7, 7]);
});

test('MemoryStore discard clears the audio and refuses further reads', async () => {
  const s = Store.createMemoryStore();
  s.append(new Int16Array(1600));
  await s.discard();
  assert.equal(s.totalBytes, 0);
  await assert.rejects(() => s.read(0, 3200), /已清空/);
  await assert.rejects(() => s.toBlob(), /已清空/);
});
