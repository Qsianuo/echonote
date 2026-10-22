/**
 * EchoNote WAV encoding (PCM / 1 channel / 16 kHz / 16 bit).
 *
 * Kept separate from the UI so the RIFF header, the data length and the
 * derived duration can be asserted by a plain Node test (PRD RG-03).
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.EchoNoteWav = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const HEADER_BYTES = 44;
  const DEFAULT_SAMPLE_RATE = 16000;

  function sampleCount(chunks) {
    return (chunks || []).reduce((sum, chunk) => sum + (chunk ? chunk.length : 0), 0);
  }

  /**
   * 只写 44 字节 RIFF 头。`totalSamples` 是总样本数（不是字节数）：
   * 原 `buildWavBuffer` 的 `36 + total * 2` 与 `total * 2` 都基于它。
   * 抽出来供流式导出复用，`buildWavBuffer` 也调用它，两者天然同源。
   */
  function buildWavHeader(totalSamples, sampleRate) {
    const rate = sampleRate || DEFAULT_SAMPLE_RATE;
    const total = totalSamples || 0;
    const buffer = new ArrayBuffer(HEADER_BYTES);
    const view = new DataView(buffer);

    const writeString = (offset, text) => {
      for (let index = 0; index < text.length; index += 1) {
        view.setUint8(offset + index, text.charCodeAt(index));
      }
    };

    writeString(0, 'RIFF');
    view.setUint32(4, 36 + total * 2, true);
    writeString(8, 'WAVE');
    writeString(12, 'fmt ');
    view.setUint32(16, 16, true); // fmt chunk size
    view.setUint16(20, 1, true); // PCM
    view.setUint16(22, 1, true); // mono
    view.setUint32(24, rate, true);
    view.setUint32(28, rate * 2, true); // byte rate
    view.setUint16(32, 2, true); // block align
    view.setUint16(34, 16, true); // bits per sample
    writeString(36, 'data');
    view.setUint32(40, total * 2, true);

    return buffer;
  }

  function buildWavBuffer(chunks, sampleRate) {
    const total = sampleCount(chunks);
    const buffer = new ArrayBuffer(HEADER_BYTES + total * 2);
    new Uint8Array(buffer, 0, HEADER_BYTES).set(new Uint8Array(buildWavHeader(total, sampleRate)));

    let offset = HEADER_BYTES;
    (chunks || []).forEach((chunk) => {
      if (!chunk || chunk.length === 0) return;
      new Int16Array(buffer, offset, chunk.length).set(chunk);
      offset += chunk.length * 2;
    });

    return buffer;
  }

  /**
   * Wraps the PCM in a Blob and drops the intermediate ArrayBuffer reference
   * so it can be collected (PRD PR-09). The Blob owns its own copy, which is
   * why the peak is ~2× the data size.
   */
  function buildWavBlob(chunks, sampleRate) {
    const buffer = buildWavBuffer(chunks, sampleRate);
    return new Blob([buffer], { type: 'audio/wav' });
  }

  /**
   * 流式导出：头是 44 字节的字面量，PCM 直接以 Blob 形式交给 Blob 构造函数。
   * 浏览器只记录引用、不复制 PCM，所以峰值不随录音时长增长（PRD PR-09）。
   */
  function buildWavBlobFromParts(header, pcm) {
    return new Blob([header, pcm], { type: 'audio/wav' });
  }

  function durationSeconds(chunks, sampleRate) {
    const rate = sampleRate || DEFAULT_SAMPLE_RATE;
    return sampleCount(chunks) / rate;
  }

  function byteLength(chunks) {
    return sampleCount(chunks) * 2;
  }

  return {
    HEADER_BYTES,
    DEFAULT_SAMPLE_RATE,
    sampleCount,
    buildWavHeader,
    buildWavBuffer,
    buildWavBlob,
    buildWavBlobFromParts,
    durationSeconds,
    byteLength,
  };
});
