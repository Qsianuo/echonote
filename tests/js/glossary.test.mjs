/**
 * 同音纠错（web/glossary.js）的行为契约。
 *
 * 与 tests/test_glossary.py 共用同一组用例文本与期望值——两端各测各的，
 * 用例重叠就是一致性保证（Python 侧跑在 app/glossary.py，JS 侧跑在原生壳里，
 * 同一段录音不能出现「一个纠正了、一个没纠正」）。
 *
 * 另外守护 web/glossary-data.js 与 models/glossary.txt 的同步：
 * 改了术语表却忘了重跑 tools/gen_glossary_data.py，这里要红。
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

import { correct, termCount } from '../../web/glossary.js';
import { TERMS } from '../../web/glossary-data.js';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');

// 与 tests/test_glossary.py 的 TERMS 保持一致。
const TERMS_FIXTURE = ['焦距', '曝光', '模板匹配', '阈值分割', '景深', '标定', '伺服'];

test('已经正确的术语一个字都不动', () => {
  assert.equal(correct('焦距是五十毫米', TERMS_FIXTURE), '焦距是五十毫米');
});

test('同音错字被换回术语', () => {
  assert.equal(correct('脚距是五十毫米', TERMS_FIXTURE), '焦距是五十毫米');
  assert.equal(correct('交距调到三十五', TERMS_FIXTURE), '焦距调到三十五');
});

test('词表之外的内容不受影响', () => {
  assert.equal(correct('今天天气怎么样', TERMS_FIXTURE), '今天天气怎么样');
});

test('四字术语容忍多一个音节', () => {
  assert.equal(correct('模板马匹配的精度', TERMS_FIXTURE), '模板匹配的精度');
});

test('两字术语不吃掉邻居的音节', () => {
  // Python 版同样的守卫：2 字术语只允许等长同音。
  assert.equal(correct('先做标定再说', TERMS_FIXTURE), '先做标定再说');
});

test('标点不会被当作音节吞进术语', () => {
  assert.equal(correct('，脚距是五十', TERMS_FIXTURE), '，焦距是五十');
});

test('长术语优先，短术语不截走开头', () => {
  assert.equal(correct('标定版贴上去', ['标定', '标定板']), '标定板贴上去');
});

test('没有术语表时原样返回', () => {
  assert.equal(correct('脚距是五十', []), '脚距是五十');
  assert.equal(correct(''), '');
});

test('纠正两次等于纠正一次', () => {
  const once = correct('脚距和暴光都要测', TERMS_FIXTURE);
  assert.equal(correct(once, TERMS_FIXTURE), once);
});

test('打包数据来自术语表，不是空壳', () => {
  // 115 词是本轮的真实规模；数量对不上说明生成脚本没跑或跑空了。
  assert.ok(termCount() >= 100, `术语只有 ${termCount()} 个`);
  assert.ok(TERMS.includes('焦距'));
  assert.ok(TERMS.includes('环形光源'));
});

test('glossary-data.js 与 models/glossary.txt 同步', () => {
  const source = readFileSync(join(root, 'models', 'glossary.txt'), 'utf8');
  const words = new Set();
  for (const raw of source.split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    for (const word of line.split(/\s+/)) if (word.length >= 2) words.add(word);
  }
  const packed = new Set(TERMS);
  const missing = [...words].filter((w) => !packed.has(w));
  assert.deepEqual(missing, [], `web/glossary-data.js 缺词，请重跑 tools/gen_glossary_data.py`);
  assert.equal(packed.size, words.size);
});

test('长文本不卡（1000 字 < 300ms）', () => {
  const sentence = '我们把脚距调到五十毫米并重新暴光，然后做模板马匹配的精度检查。';
  const text = sentence.repeat(40);
  const started = Date.now();
  const out = correct(text, TERMS_FIXTURE);
  const cost = Date.now() - started;
  assert.ok(out.includes('焦距调到'));
  assert.ok(cost < 300, `耗时 ${cost}ms`);
});
