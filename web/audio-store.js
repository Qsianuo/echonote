/**
 * EchoNote 音频存储（v0.12.0）。
 *
 * `PcmLedger` 只做字节记账：录音时每次写入只需 O(1) 累加，替代原先
 * 每次 `Wav.byteLength(chunks)` 的 O(n) 遍历。纯逻辑，无浏览器 API，
 * 因此可以直接用 Node 测试。
 *
 * `createMemoryStore` 是 OPFS 不可用时的降级实现，接口与 app.js 里的
 * OPFS store 保持一致（append/finish/read/toBlob/discard/totalBytes），
 * 同样不依赖浏览器 API，所以降级路径也有自动化覆盖。
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
        // 只记账，永不拒收：预算超了也照收，避免静默截断录音。
        accept(chunk) {
          if (!(chunk instanceof Int16Array)) {
            throw new TypeError('PcmLedger.accept() expects an Int16Array');
          }
          bytes += chunk.length * 2;
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

  /**
   * 降级实现：数据留在内存数组里，长录音会重新变成 O(时长) 占用。
   * 与 OPFS store 同接口，`fallback` 恒为 true，调用方据此提示用户。
   */
  function createMemoryStore() {
    const chunks = [];
    let bytes = 0;
    let discarded = false;

    function assertReadable() {
      if (discarded) throw new Error('录音已清空，无法读取');
    }

    const store = {
      fallback: true,

      get totalBytes() {
        return bytes;
      },

      get sampleCount() {
        return bytes / 2;
      },

      append(chunk) {
        if (!(chunk instanceof Int16Array)) {
          throw new TypeError('append() expects an Int16Array');
        }
        if (discarded || chunk.length === 0) return;
        // 拷贝一份：调用方（sendFrame）随后还要把原 buffer 发给 WebSocket。
        chunks.push(chunk.slice());
        bytes += chunk.byteLength;
      },

      async finish() {
        /* 内存里没有句柄需要收尾 */
      },

      flush() {
        /* 内存实现无需刷盘 */
      },

      /**
       * 按字节区间取一段 PCM。直接从已持有的 chunk 切片，**不再经过 toBlob()**：
       * 降级模式下录音全在内存里，一次 toBlob() 会把整段（60 min ≈ 180 MB）
       * 重新拷一遍，而回放每 30 s 就要读一次，等于把内存峰值钉在录音全长上。
       * 逐块按字节拷贝的语义与 OPFS 版 `file.slice()` 完全一致。
       */
      async read(from, to) {
        assertReadable();
        const start = Math.max(0, Math.min(bytes, Math.floor(Number(from) || 0)));
        const end = Math.max(start, Math.min(bytes, Math.floor(Number(to) || 0)));
        const out = new Uint8Array(end - start);
        let chunkStart = 0;
        let written = 0;
        for (const chunk of chunks) {
          const chunkBytes = chunk.byteLength;
          const chunkEnd = chunkStart + chunkBytes;
          if (chunkEnd <= start) {
            chunkStart = chunkEnd;
            continue;
          }
          if (chunkStart >= end) break;
          // Int16Array 只是写入时的载体；读出来一律按字节，保持与落盘字节一致。
          const view = new Uint8Array(chunk.buffer, chunk.byteOffset, chunkBytes);
          const fromInChunk = Math.max(0, start - chunkStart);
          const toInChunk = Math.min(chunkBytes, end - chunkStart);
          out.set(view.subarray(fromInChunk, toInChunk), written);
          written += toInChunk - fromInChunk;
          chunkStart = chunkEnd;
        }
        return out.buffer;
      },

      async toBlob() {
        assertReadable();
        return new Blob(chunks);
      },

      async discard() {
        discarded = true;
        chunks.length = 0;
        bytes = 0;
      },
    };

    return store;
  }

  return { PcmLedger, createMemoryStore };
});
