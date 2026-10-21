/**
 * EchoNote 音频存储（v0.12.0）。
 *
 * `PcmLedger` 只做字节记账：录音时每次写入只需 O(1) 累加，替代原先
 * 每次 `Wav.byteLength(chunks)` 的 O(n) 遍历。纯逻辑，无浏览器 API，
 * 因此可以直接用 Node 测试。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.EchoNoteAudioStore = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const PcmLedger = {
    create(options) {
      let bytes = 0;
      const maxBytes = options.maxBytes;

      return {
        accept(chunk) {
          if (!(chunk instanceof Int16Array)) {
            throw new TypeError('PcmLedger.accept() expects an Int16Array');
          }
          const add = chunk.length * 2;
          if (bytes + add > maxBytes) {
            return { accepted: false, droppedBytes: add };
          }
          bytes += add;
          return { accepted: true, droppedBytes: 0 };
        },

        get byteLength() {
          return bytes;
        },

        get sampleCount() {
          return bytes / 2;
        },

        // 严格大于：与 app.js 既有 checkMemory 的 `bytes > MEMORY_BUDGET_BYTES` 一致，
        // 正好用满预算不算超。
        get overBudget() {
          return bytes > maxBytes;
        },

        reset() {
          bytes = 0;
        },
      };
    },
  };

  return { PcmLedger };
});
