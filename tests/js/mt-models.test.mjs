/**
 * 「离线翻译模型」设置块的文案与进度换算（web/mt-models.js）。
 *
 * 只测纯函数：DOM 装配要靠 WebView 远程调试验证，这里锁住的是"用户在
 * 进度条旁边看到的那行字"——它错了，用户会以为下载卡住了。
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import { formatMb, progressLabel, progressRatio } from '../../web/mt-models.js';

const MB = 1024 * 1024;

test('formatMb：不足 1 GB 用整 MB，超过用一位小数 GB', () => {
  assert.equal(formatMb(0), '0 MB');
  assert.equal(formatMb(-5), '0 MB');
  assert.equal(formatMb(154_922_184), '148 MB');
  assert.equal(formatMb(236_800_000), '226 MB');
  assert.equal(formatMb(2 * 1024 * MB), '2.0 GB');
});

test('download 阶段：有总量给百分比，没总量只给已下载量', () => {
  assert.equal(
    progressLabel({ phase: 'download', received: 77 * MB, total: 148 * MB }),
    '下载 52% · 77 MB / 148 MB',
  );
  assert.equal(progressLabel({ phase: 'download', received: 5 * MB }), '下载 5 MB');
  // 超出总量时封顶 100%，不出现 103%
  assert.equal(
    progressLabel({ phase: 'download', received: 160 * MB, total: 148 * MB }),
    '下载 100% · 160 MB / 148 MB',
  );
});

test('其余阶段的文案', () => {
  assert.equal(progressLabel({ phase: 'resolve' }), '准备中…');
  assert.equal(progressLabel({ phase: 'verify' }), '校验中…');
  assert.equal(progressLabel({ phase: 'unzip', entries: 4 }), '解压中 4 个文件');
  assert.equal(progressLabel({ phase: 'unzip' }), '解压中…');
  assert.equal(progressLabel({ phase: 'done' }), '');
  assert.equal(progressLabel(null), '');
});

test('progressRatio：只在 download 且有总量时给数，并夹在 0~1', () => {
  assert.equal(progressRatio({ phase: 'download', received: 74, total: 148 }), 0.5);
  assert.equal(progressRatio({ phase: 'download', received: 200, total: 148 }), 1);
  assert.equal(progressRatio({ phase: 'download', received: -3, total: 148 }), 0);
  assert.equal(progressRatio({ phase: 'download', received: 5 }), null);
  assert.equal(progressRatio({ phase: 'unzip', received: 5, total: 10 }), null);
  assert.equal(progressRatio(null), null);
});

test('copy 阶段（本地导入）用「导入」而不是「下载」', () => {
  assert.equal(
    progressLabel({ phase: 'copy', received: 74 * MB, total: 148 * MB }),
    '导入 50% · 74 MB / 148 MB',
  );
  assert.equal(progressLabel({ phase: 'copy', received: 3 * MB }), '导入 3 MB');
  // 复制阶段不该画进度条：它是本地 IO，秒级完成，抖动无意义
  assert.equal(progressRatio({ phase: 'copy', received: 74, total: 148 }), null);
});
