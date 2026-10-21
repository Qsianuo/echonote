/**
 * EchoNote 播放预排（v0.12.0）。
 *
 * `PlaybackScheduler` 只算「下一段该排在什么时刻开始」，`windowFor` 只把
 * 播放位置换算成待解码的字节区间。纯逻辑，无 WebAudio API，因此可以直接
 * 用 Node 测试。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.EchoNotePlayback = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const PlaybackScheduler = {
    create({ leadS = 0.02 } = {}) {
      // 0 表示队列为空，首次 nextStartAt 会落到 currentTime + leadS 上。
      let nextTime = 0;

      return {
        // 队列没断就贴着上一段的结尾排，断了（currentTime 已越过队列尾）
        // 就跟当前时间走，避免把积压的时间一次性补播。
        nextStartAt(currentTime) {
          return Math.max(currentTime + leadS, nextTime || 0);
        },

        advance(startAt, duration) {
          nextTime = startAt + duration;
        },

        reset(atCurrentTime) {
          nextTime = atCurrentTime;
        },

        get nextTime() {
          return nextTime;
        },
      };
    },
  };

  // Int16 单声道 PCM：每秒字节数。两次除 2 再乘回 2 是为了让偏移量对齐到
  // 采样点边界，避免解码出半个 sample。
  function windowFor({ positionS, windowS, sampleRate, totalBytes }) {
    const bytesPerSecond = sampleRate * 2;
    const startS = Math.max(0, positionS);

    const fromByte = Math.max(0, Math.floor(startS * bytesPerSecond / 2) * 2);
    const toByte = Math.min(totalBytes, Math.ceil((startS + windowS) * bytesPerSecond / 2) * 2);

    return { fromByte, toByte };
  }

  return { PlaybackScheduler, windowFor };
});
