/**
 * 非同音错词表在浏览器侧的行为（web/glossary.js 的 applyFixes）。
 *
 * 与 tests/test_term_fixes.py 是同一份契约的两端实现：桌面端跑
 * app/term_fixes.py，原生壳跑这里。判据必须一致，否则同一段录音在两个平台
 * 会出现「一个纠正了、一个没纠正」。
 *
 * 另外守着 web/glossary-data.js 与 models/term-fixes.txt 的同步——改了表却
 * 忘了重跑 tools/gen_glossary_data.py，这里要红。
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

import { applyFixes } from '../../web/glossary.js';
import { TERM_FIXES } from '../../web/glossary-data.js';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');

test('打包数据与 models/term-fixes.txt 一致（长串在前）', () => {
  const raw = readFileSync(join(root, 'models', 'term-fixes.txt'), 'utf8');
  const parsed = raw
    .split('\n')
    .map((line) => line.split('#')[0].trim())
    .filter(Boolean)
    .map((line) => line.split('=>').map((part) => part.trim()))
    .filter((parts) => parts.length === 2 && parts[0] && parts[1])
    .sort((a, b) => b[0].length - a[0].length);

  assert.ok(parsed.length > 0, '错词表是空的');
  assert.deepEqual(
    TERM_FIXES.map(([wrong, right]) => `${wrong}=>${right}`),
    parsed.map(([wrong, right]) => `${wrong}=>${right}`)
  );
});

test('魔方匹配 → 模板匹配', () => {
  assert.equal(applyFixes('这边用魔方匹配找特征'), '这边用模板匹配找特征');
});

test('开/闭预算 → 开/闭运算，长串先吃', () => {
  assert.equal(applyFixes('再加一个闭预算'), '再加一个闭运算');
  assert.equal(applyFixes('先做开预算'), '先做开运算');
});

test('光秃秃的「预算」不动', () => {
  const text = '这个月的预算还没批';
  assert.equal(applyFixes(text), text);
});

test('表外的文字一个字不改', () => {
  for (const text of ['今天天气不错', '', '模板匹配本来就是对的']) {
    assert.equal(applyFixes(text), text);
  }
});

test('同一句里多处错串都会被替换', () => {
  assert.equal(
    applyFixes('魔方匹配之后做二次化，再开预算'),
    '模板匹配之后做二值化，再开运算'
  );
});
