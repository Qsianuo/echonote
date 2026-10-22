import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const W = require('../../web/waveform.js');

function makeCtx() {
  const ctx = {
    gradients: [],
    clears: [],
    createLinearGradient(x0, y0, x1, y1) {
      const g = {
        x0,
        y0,
        x1,
        y1,
        stops: [],
        addColorStop(offset, color) {
          this.stops.push([offset, color]);
        },
      };
      ctx.gradients.push(g);
      return g;
    },
    clearRect(x, y, w, h) {
      ctx.clears.push({ x, y, w, h });
    },
    beginPath() {},
    moveTo() {},
    lineTo() {},
    stroke() {},
    fillRect() {},
  };
  return ctx;
}

function makeCanvas(ctx) {
  return { width: 0, height: 0, getContext: () => ctx };
}

function makeHost(width, height) {
  const host = {
    rect: { width, height },
    calls: 0,
    getBoundingClientRect() {
      host.calls += 1;
      return { width: host.rect.width, height: host.rect.height };
    },
  };
  return host;
}

function makeAnalyser(binCount) {
  const analyser = {
    frequencyBinCount: binCount,
    buffers: [],
    getByteTimeDomainData(buffer) {
      analyser.buffers.push(buffer);
      buffer.fill(128);
    },
  };
  return analyser;
}

/* ───────────────────────────── RowIndex ───────────────────────────── */

test('RowIndex returns the stored entry by id', () => {
  const idx = W.RowIndex.create();
  const entry = { id: '1-1', row: null };
  idx.set('1-1', entry);
  assert.equal(idx.get('1-1'), entry);
  assert.equal(idx.get('9-9'), undefined);
  assert.equal(idx.size, 1);
});

test('RowIndex overwrite does not grow size', () => {
  const idx = W.RowIndex.create();
  idx.set('1-1', { id: '1-1' });
  idx.set('1-1', { id: '1-1' });
  assert.equal(idx.size, 1);
});

test('clear empties the index', () => {
  const idx = W.RowIndex.create();
  idx.set('1-1', {});
  idx.clear();
  assert.equal(idx.size, 0);
  assert.equal(idx.get('1-1'), undefined);
});

test('delete drops a single row and keeps the others', () => {
  const idx = W.RowIndex.create();
  idx.set('1-1', { id: '1-1' });
  idx.set('2-2', { id: '2-2' });
  idx.delete('1-1');
  assert.equal(idx.get('1-1'), undefined);
  assert.equal(idx.get('2-2').id, '2-2');
  assert.equal(idx.size, 1);
});

/* ───────────────────────────── Waveform ───────────────────────────── */

test('geometry is reused until invalidated', () => {
  const ctx = makeCtx();
  const canvas = makeCanvas(ctx);
  const host = makeHost(300, 80);
  const wf = W.Waveform.create({ canvas, host, dprProvider: () => 2 });

  wf.measure();
  assert.equal(host.calls, 1);
  assert.equal(canvas.width, 600);
  assert.equal(canvas.height, 160);

  wf.measure();
  assert.equal(host.calls, 2);
});

test('draw never reads layout', () => {
  const ctx = makeCtx();
  const canvas = makeCanvas(ctx);
  const host = makeHost(300, 80);
  const wf = W.Waveform.create({ canvas, host, dprProvider: () => 2 });
  const analyser = makeAnalyser(128);

  wf.measure();
  assert.equal(host.calls, 1);
  for (let i = 0; i < 10; i += 1) wf.draw(analyser);
  wf.drawIdle();
  assert.equal(host.calls, 1, 'draw()/drawIdle() 不得触发布局');
});

test('draw clears with the cached bitmap size', () => {
  const ctx = makeCtx();
  const canvas = makeCanvas(ctx);
  const host = makeHost(300, 80);
  const wf = W.Waveform.create({ canvas, host, dprProvider: () => 2 });
  wf.measure();
  wf.draw(makeAnalyser(128));
  assert.deepEqual(ctx.clears[0], { x: 0, y: 0, w: 600, h: 160 });
});

test('sample buffer is allocated once and reused across frames', () => {
  const ctx = makeCtx();
  const canvas = makeCanvas(ctx);
  const host = makeHost(300, 80);
  const wf = W.Waveform.create({ canvas, host, dprProvider: () => 2 });
  const analyser = makeAnalyser(128);
  wf.measure();

  wf.draw(analyser);
  wf.draw(analyser);
  wf.draw(analyser);
  assert.equal(analyser.buffers.length, 3);
  assert.equal(analyser.buffers[0], analyser.buffers[2], '应复用同一个 Uint8Array');
  assert.equal(analyser.buffers[0].length, 128);

  // 换一支 binCount 更大的 analyser 才重新分配：新缓冲必须按新容量重建，
  // 不能把 128 的旧缓冲塞给 256 的 analyser（会截断一半频域数据）。
  const wider = makeAnalyser(256);
  wf.draw(wider);
  assert.equal(wider.buffers.length, 1);
  assert.equal(wider.buffers[0].length, 256, 'binCount 变大后缓冲必须按新容量重建');
  assert.notEqual(wider.buffers[0], analyser.buffers[0], '不能复用旧容量的缓冲');
  // 渐变只跟宽度有关：binCount 变化不该白白重建渐变（下一次 draw 仍用同一个）。
  assert.equal(ctx.gradients.length, 1, 'binCount 变化不影响渐变缓存');
});

test('gradient is created once and invalidated by measure', () => {
  const ctx = makeCtx();
  const canvas = makeCanvas(ctx);
  const host = makeHost(300, 80);
  const wf = W.Waveform.create({ canvas, host, dprProvider: () => 2 });
  const analyser = makeAnalyser(128);
  wf.measure();

  wf.draw(analyser);
  wf.draw(analyser);
  assert.equal(ctx.gradients.length, 1, '同一尺寸下渐变只建一次');
  assert.equal(ctx.gradients[0].x1, 600);
  assert.equal(ctx.gradients[0].stops.length, 3);

  host.rect = { width: 450, height: 80 };
  wf.measure();
  wf.draw(analyser);
  assert.equal(ctx.gradients.length, 2, 'resize 后渐变必须重建');
  assert.equal(ctx.gradients[1].x1, 900);
  assert.notEqual(ctx.gradients[1], ctx.gradients[0], '失效后必须换成新渐变对象');
});

test('destroy makes drawing a no-op', () => {
  const ctx = makeCtx();
  const canvas = makeCanvas(ctx);
  const host = makeHost(300, 80);
  const wf = W.Waveform.create({ canvas, host, dprProvider: () => 1 });
  const analyser = makeAnalyser(128);
  wf.measure();
  wf.destroy();
  wf.draw(analyser);
  wf.drawIdle();
  assert.equal(analyser.buffers.length, 0);
  assert.equal(ctx.clears.length, 0);
});
