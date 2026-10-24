/**
 * VAD 移植一致性测试：同一份音频，Python 版（app/vad.py）与 JS 版
 * （mobile/src/vad.js）必须切出相同的句数和相同的起止时间（±1 帧）。
 *
 * 运行：
 *   cd mobile && npm test
 * 生成基准（改了 Python 版或样本后重跑）：
 *   ../.venv/Scripts/python.exe test/gen_fixtures.py
 *
 * 实现在 web/native-vad.js（随原生壳一起打包，桌面环境加载无害）。
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { Segmenter } from '../../web/native-vad.js';

const here = dirname(fileURLToPath(import.meta.url));
const fixtures = JSON.parse(readFileSync(join(here, 'fixtures.json'), 'utf-8'));

const TOLERANCE_S = 0.03 + 0.001; // 一帧（30 ms）+ 浮点余量

function feedAll(segmenter, audio) {
  const events = [];
  const frame = Math.floor((fixtures.sampleRate * 30) / 1000);
  for (let start = 0; start + frame <= audio.length; start += frame) {
    events.push(...segmenter.push(new Float32Array(audio.slice(start, start + frame))));
  }
  const tail = segmenter.flush();
  if (tail) events.push(tail);
  return events;
}

let checked = 0;
for (const { name, overrides, audio, events: expected } of fixtures.cases) {
  const got = feedAll(new Segmenter(overrides), audio);

  assert.equal(
    got.length,
    expected.length,
    `${name}: 句数不一致（JS ${got.length} vs Python ${expected.length}）`
  );
  got.forEach((event, i) => {
    assert.ok(
      Math.abs(event.start - expected[i].start) <= TOLERANCE_S,
      `${name}[${i}].start: JS ${event.start.toFixed(3)} vs Python ${expected[i].start}`
    );
    assert.ok(
      Math.abs(event.end - expected[i].end) <= TOLERANCE_S,
      `${name}[${i}].end: JS ${event.end.toFixed(3)} vs Python ${expected[i].end}`
    );
  });
  checked += got.length;
  console.log(`ok  ${name}: ${got.length} 段一致`);
}

console.log(`\n${fixtures.cases.length} 组样本、${checked} 个段落全部对齐。`);
