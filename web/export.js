/**
 * EchoNote export formatting.
 *
 * Pure functions only: no DOM, no Blob, no clock reads. The app passes a
 * fixed `startedAt`/`endedAt`, which is what makes the golden-sample test
 * (PRD RG-15) deterministic and keeps the metadata semantics honest —
 * "录音时间" is when recording started, not when export was clicked.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.EchoNoteExport = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const pad = (value, size = 2) => String(value).padStart(size, '0');

  /** `mm:ss` (minutes keep counting past 60, as the transcript shows). */
  function formatClock(totalSeconds) {
    const seconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
    const mm = pad(Math.floor(seconds / 60));
    const ss = pad(seconds % 60);
    return `${mm}:${ss}`;
  }

  /** `HH:MM:SS,mmm` for SRT cues. */
  function srtTime(totalSeconds) {
    const ms = Math.max(0, Math.round((Number(totalSeconds) || 0) * 1000));
    const hours = Math.floor(ms / 3600000);
    const minutes = Math.floor((ms % 3600000) / 60000);
    const seconds = Math.floor((ms % 60000) / 1000);
    const millis = ms % 1000;
    return `${pad(hours)}:${pad(minutes)}:${pad(seconds)},${pad(millis, 3)}`;
  }

  /** Local `YYYY-MM-DD HH:mm:ss`, stable across time zones. */
  function formatDateTime(date) {
    return (
      `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
      `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
    );
  }

  /** Local `YYYYMMDD-HHmm`, matching the metadata time zone. */
  function formatFileStamp(date) {
    return (
      `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-` +
      `${pad(date.getHours())}${pad(date.getMinutes())}`
    );
  }

  function languageLabel(code, languages) {
    const list = Array.isArray(languages) ? languages : [];
    const hit = list.find((item) => item && item.code === code);
    return hit ? hit.label : code || '自动识别';
  }

  function buildMetadata(input) {
    const options = input || {};
    const started = options.startedAt instanceof Date ? options.startedAt : new Date();
    const ended = options.endedAt instanceof Date ? options.endedAt : started;
    const durationMs = Math.max(0, ended.getTime() - started.getTime());
    const code = options.language || 'auto';

    return {
      startedAt: started,
      endedAt: ended,
      durationMs,
      durationSeconds: durationMs / 1000,
      startedLabel: formatDateTime(started),
      endedLabel: formatDateTime(ended),
      durationLabel: formatClock(durationMs / 1000),
      model: options.model || 'unknown',
      languageCode: code,
      languageLabel: languageLabel(code, options.languages),
    };
  }

  function finalLines(finals) {
    return (finals || []).map((item) => `[${formatClock(item.start)}] ${item.text}`);
  }

  function buildTxt(meta, finals) {
    return [
      'EchoNote 转写记录',
      `录音时间：${meta.startedLabel}`,
      `结束时间：${meta.endedLabel}`,
      `时长：${meta.durationLabel}`,
      `模型：${meta.model}`,
      `语言：${meta.languageLabel}（${meta.languageCode}）`,
      '',
      ...finalLines(finals),
      '',
    ].join('\n');
  }

  function buildMarkdown(meta, finals) {
    return [
      '# EchoNote 转写记录',
      '',
      `- **录音时间**：${meta.startedLabel}`,
      `- **结束时间**：${meta.endedLabel}`,
      `- **时长**：${meta.durationLabel}`,
      `- **模型**：${meta.model}`,
      `- **语言**：${meta.languageLabel}（${meta.languageCode}）`,
      '',
      '---',
      '',
      ...(finals || []).map((item) => `- \`${formatClock(item.start)}\` ${item.text}`),
      '',
    ].join('\n');
  }

  /**
   * SRT uses *absolute* recording time (PRD FR-040 §9.6), so the first cue
   * starts where the first final actually started, not at 00:00:00.
   */
  function buildSrt(finals) {
    return (finals || [])
      .map((item, index) => {
        const start = Math.max(0, Number(item.start) || 0);
        const end = Math.max(Number(item.end) || 0, start + 1.5);
        return `${index + 1}\n${srtTime(start)} --> ${srtTime(end)}\n${item.text}\n`;
      })
      .join('\n');
  }

  function fileName(meta, extension) {
    return `echonote-${formatFileStamp(meta.startedAt)}.${extension}`;
  }

  return {
    formatClock,
    srtTime,
    formatDateTime,
    formatFileStamp,
    languageLabel,
    buildMetadata,
    buildTxt,
    buildMarkdown,
    buildSrt,
    fileName,
  };
});
