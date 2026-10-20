import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const Expo = require('../../web/export.js');

const here = path.dirname(fileURLToPath(import.meta.url));
const fixtures = path.resolve(here, '..', 'fixtures', 'export-v1');

// Fixed clock + fixed finals: this is what makes RG-15 a byte-for-byte test
// instead of a "looks right" test.
const META_INPUT = {
  startedAt: new Date(2026, 9, 20, 23, 59, 12),
  endedAt: new Date(2026, 9, 21, 0, 4, 3),
  model: 'small',
  language: 'zh',
  languages: [{ code: 'zh', label: '中文' }],
};

const FINALS = [
  { text: '第一段。', start: 1.2, end: 4.08 },
  { text: '第二段。', start: 5.04, end: 8.16 },
  { text: '跨零点后的一段。', start: 250.5, end: 254.0 },
];

const meta = Expo.buildMetadata(META_INPUT);

function golden(name) {
  return readFileSync(path.join(fixtures, name), 'utf8');
}

test('formatClock keeps counting minutes past an hour', () => {
  assert.equal(Expo.formatClock(0), '00:00');
  assert.equal(Expo.formatClock(65), '01:05');
  assert.equal(Expo.formatClock(5400), '90:00');
  assert.equal(Expo.formatClock(-4), '00:00');
});

test('srtTime emits HH:MM:SS,mmm', () => {
  assert.equal(Expo.srtTime(0), '00:00:00,000');
  assert.equal(Expo.srtTime(1.2), '00:00:01,200');
  assert.equal(Expo.srtTime(3661.5), '01:01:01,500');
});

test('metadata uses the recording start, not the export time (FR-037)', () => {
  assert.equal(meta.startedLabel, '2026-10-20 23:59:12');
  assert.equal(meta.endedLabel, '2026-10-21 00:04:03');
  assert.equal(meta.durationLabel, '04:51');
  assert.equal(meta.languageLabel, '中文');
  assert.equal(meta.model, 'small');
});

test('file name uses the recording start timestamp', () => {
  assert.equal(Expo.fileName(meta, 'txt'), 'echonote-20261020-2359.txt');
});

test('TXT matches the golden sample byte for byte', () => {
  assert.equal(Expo.buildTxt(meta, FINALS), golden('transcript.txt'));
});

test('Markdown matches the golden sample byte for byte', () => {
  assert.equal(Expo.buildMarkdown(meta, FINALS), golden('transcript.md'));
});

test('SRT matches the golden sample byte for byte', () => {
  assert.equal(Expo.buildSrt(FINALS), golden('subtitles.srt'));
});

test('SRT starts at the absolute recording time, never at zero', () => {
  const srt = Expo.buildSrt([{ text: 'x', start: 12.5, end: 14.0 }]);
  assert.match(srt, /00:00:12,500 --> 00:00:14,000/);
});

test('SRT enforces a 1.5 s minimum cue length', () => {
  const srt = Expo.buildSrt([{ text: 'x', start: 1.0, end: 1.2 }]);
  assert.match(srt, /00:00:01,000 --> 00:00:02,500/);
});

test('empty transcript exports nothing', () => {
  assert.equal(Expo.buildSrt([]), '');
});

test('language label falls back to the raw code', () => {
  assert.equal(Expo.languageLabel('ja', undefined), 'ja');
  assert.equal(Expo.languageLabel('zh', [{ code: 'zh', label: '中文' }]), '中文');
});
