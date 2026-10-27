/**
 * 译文术语修复（web/mt-repair.js）的行为契约。
 *
 * 每条「应该修」的用例都抄自真实输出：句子的英文原文与错译来自
 * `python tools/mt_term_eval.py model-dist/mt-onnx/mt-en-zh --backend onnx`
 * 的实测结果（桌面 ctranslate2 给出的错译一字不差）。改规则前先跑那个脚本，
 * 没有实测错译就不要加规则——这一层宁可不修，也不能把对的改错。
 *
 * 这一层是两端共用的：桌面端译文由 Python 产出、安卓端由 Kotlin 产出，
 * 但都经过 `app.js` 的 onFinal / onRefine，所以修一次两端都吃。
 */

import assert from 'node:assert/strict';
import test from 'node:test';

// classic script：执行即把接口挂到 globalThis，不导出任何东西。
import '../../web/mt-repair.js';

const { repair } = globalThis.EchoMTRepair;

test('ring light 被译成「戒指灯」时修回「环形灯」', () => {
  const source = 'The ring light is too dim, we need to light it again.';
  // 实测输出（ONNX 贪心）
  assert.equal(repair(source, '戒指灯太暗了,我们得再点一次'), '环形灯太暗了,我们得再点一次');
});

test('telecentric lens 被拆错时修回「远心镜头」', () => {
  const source = 'We use a telecentric lens for measurement.';
  assert.equal(repair(source, '我们用远程中枢镜头测量'), '我们用远心镜头测量');
  // ctranslate2 的写法略有不同，同一张错译表也要覆盖
  assert.equal(repair(source, '我们用远程中心镜头来测量'), '我们用远心镜头来测量');
});

test('exposure time 被译成「接触时间」时修回「曝光时间」', () => {
  const source = 'The exposure time should not exceed ten milliseconds.';
  assert.equal(repair(source, '接触时间不应超过10毫秒。'), '曝光时间不应超过10毫秒。');
});

test('depth of field 只剩「深度」时补成「景深」', () => {
  const source = 'The depth of field of this lens is too shallow.';
  assert.equal(repair(source, '镜头的深度太浅了'), '镜头的景深太浅了');
});

test('原文没有这个术语时，译文一个字都不动', () => {
  // 这是整层的安全阀：没提 ring light，就不许把「戒指灯」改掉
  const source = 'The lamp on the table is broken.';
  assert.equal(repair(source, '桌上的戒指灯坏了'), '桌上的戒指灯坏了');
  assert.equal(repair(source, '镜头的深度太浅了'), '镜头的深度太浅了');
});

test('译文已经正确时不做二次加工', () => {
  const source = 'The ring light is too dim.';
  assert.equal(repair(source, '环形灯太暗了'), '环形灯太暗了');
  assert.equal(repair(source, '景深太浅了'), '景深太浅了');
});

test('一句里出现两处「深度」时只改第一处', () => {
  const source = 'The depth of field is shallow, and the depth of the box is 20 mm.';
  assert.equal(
    repair(source, '镜头的深度太浅,箱子的深度是20毫米'),
    '镜头的景深太浅,箱子的深度是20毫米'
  );
});

test('空输入原样返回，不抛异常', () => {
  assert.equal(repair('The ring light is dim.', ''), '');
  assert.equal(repair('', '戒指灯太暗了'), '戒指灯太暗了');
  assert.equal(repair(undefined, undefined), undefined);
});

test('大小写与复数都认', () => {
  assert.equal(repair('Two Ring Lights are broken.', '两个戒指灯坏了'), '两个环形灯坏了');
  assert.equal(repair('Check the telecentric lenses.', '检查远程镜头'), '检查远心镜头');
});
