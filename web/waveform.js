/**
 * EchoNote 波形渲染 + 字幕行索引（v0.12.0）。
 *
 * `Waveform` 把 canvas 几何量测与逐帧绘制彻底分开：
 * - `measure()` 是全模块**唯一**读布局的地方（`getBoundingClientRect` + dpr），
 *   只在初始化与 `resize` 时调用，顺带重建位图尺寸、让渐变失效；
 * - `draw()` 只读缓存值，每帧不再触碰布局，也不会重新分配 `Uint8Array` /
 *   渐变对象，因此不会跟字幕滚动交替读写布局造成 layout thrashing。
 *
 * `RowIndex` 是字幕行的 `Map` 索引，替代每次遍历 `el.finals.children` 的
 * 线性查找：长会话里 `appendFinal` / `onRefine` / 高亮切换都不会再退化成 O(n²)。
 *
 * 纯逻辑（除 canvas 之外无 DOM 依赖），可直接用 Node 测试。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.EchoNoteWaveform = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const Waveform = {
    /**
     * @param {object} options
     * @param {HTMLCanvasElement} options.canvas 绘制目标（位图尺寸由 measure 写）
     * @param {Element} options.host 量测对象，通常就是 canvas 自己
     * @param {() => number} [options.dprProvider] devicePixelRatio 来源
     */
    create(options) {
      const canvas = options.canvas;
      const host = options.host || canvas;
      const dprProvider = options.dprProvider || (() => 1);

      let ctx = canvas.getContext('2d');
      let dpr = 1;
      let width = 1;
      let height = 1;
      // 复用缓冲：binCount 不变就一直用同一个 Uint8Array，避免每帧 GC 压力。
      let bins = null;
      // null 表示失效（尺寸变了 / 还没量过），下一次 draw 时按新宽度重建。
      let gradient = null;

      /**
       * 全模块唯一读布局的地方。只在初始化与 resize 时调用。
       * 重建 canvas 像素尺寸会清空绘图状态，所以渐变一并作废。
       */
      function measure() {
        dpr = dprProvider() || 1;
        const rect = host.getBoundingClientRect();
        width = Math.max(1, Math.floor(rect.width * dpr));
        height = Math.max(1, Math.floor(rect.height * dpr));
        if (canvas.width !== width || canvas.height !== height) {
          canvas.width = width;
          canvas.height = height;
        }
        gradient = null;
        return { dpr, width, height };
      }

      function ensureBins(count) {
        if (!bins || bins.length !== count) bins = new Uint8Array(count);
        return bins;
      }

      function ensureGradient() {
        if (!gradient) {
          gradient = ctx.createLinearGradient(0, 0, width, 0);
          gradient.addColorStop(0, 'rgba(100, 255, 218, 0.25)');
          gradient.addColorStop(0.5, 'rgba(100, 255, 218, 0.95)');
          gradient.addColorStop(1, 'rgba(100, 255, 218, 0.25)');
        }
        return gradient;
      }

      /** 画一帧时域波形。只读缓存，不得出现 getBoundingClientRect。 */
      function draw(analyser) {
        if (!ctx || !analyser) return;
        const data = ensureBins(analyser.frequencyBinCount);
        analyser.getByteTimeDomainData(data);

        ctx.clearRect(0, 0, width, height);
        ctx.strokeStyle = ensureGradient();
        ctx.lineWidth = 2 * dpr;
        ctx.beginPath();
        const step = width / data.length;
        for (let i = 0; i < data.length; i += 1) {
          const value = data[i] / 128 - 1;
          const x = i * step;
          const y = height / 2 + value * (height * 0.34);
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.stroke();
      }

      /** 静默态的定格波形（一条中轴线 + 刻度）。同样只用缓存几何。 */
      function drawIdle() {
        if (!ctx) return;
        ctx.clearRect(0, 0, width, height);

        const middle = height / 2;
        ctx.strokeStyle = 'rgba(100, 255, 218, 0.38)';
        ctx.lineWidth = 2 * dpr;
        ctx.beginPath();
        ctx.moveTo(0, middle);
        ctx.lineTo(width, middle);
        ctx.stroke();

        ctx.fillStyle = 'rgba(100, 255, 218, 0.35)';
        const step = width / 28;
        for (let i = 1; i < 28; i += 1) {
          const x = i * step;
          const amp = (i % 3 === 0 ? 6 : 3) * dpr;
          ctx.fillRect(x, middle - amp, Math.max(1, dpr), amp * 2);
        }
      }

      /** 释放缓存引用，之后的 draw/drawIdle 变成空操作。 */
      function destroy() {
        ctx = null;
        bins = null;
        gradient = null;
      }

      return { measure, draw, drawIdle, destroy };
    },
  };

  const RowIndex = {
    create() {
      const map = new Map();
      return {
        set(id, entry) {
          map.set(id, entry);
        },
        get(id) {
          return map.get(id);
        },
        delete(id) {
          map.delete(id);
        },
        clear() {
          map.clear();
        },
        get size() {
          return map.size;
        },
      };
    },
  };

  return { Waveform, RowIndex };
});
