/**
 * 译文术语修复：把 77M 小模型翻出来的「假朋友」换回正确术语。
 *
 * 为什么需要这一层
 * ----------------
 * 桌面端跑 ctranslate2（beam 4 + no_repeat_ngram）、安卓端跑 ONNX int8
 * （贪心），两套完全独立的推理栈在 `tools/mt_term_eval.py` 的 12 条术语句上
 * 给出**同样的 4 条错译**：
 *
 *   ring light       → 戒指灯       （ring 按「戒指」直译）
 *   telecentric lens → 远程中心镜头 （tele+centric 拆错）
 *   exposure time    → 接触时间     （exposure 按「接触」直译）
 *   depth of field   → 镜头的深度   （field 被丢掉）
 *
 * 同一批句子在两套栈上都错，说明不是量化、不是词表、也不是解码策略，而是
 * 77M Marian 的短语表本身缺这些搭配。换模型能解决，但 77M 的价值就是小而快，
 * 课堂上 50~100 ms 出译文；为了 4 个词换 600M 模型不划算。
 *
 * 所以做**定向修复**，判据卡得很死，宁可漏修也不误修：
 *
 *  1. 只在「译成中文」时生效（英文译文不用管）；
 *  2. 只在这个术语**真的出现在英文原文里**时才动手——原文没有 ring light，
 *     译文里出现「戒指灯」也一个字不改；
 *  3. 译文里已经含正确术语的直接放过（不做二次加工）；
 *  4. 只替换已知的错误写法（实测得到的坏串），不做任何模糊匹配。
 *
 * 规则全部来自实测输出，没有一条是「顺手加的」。加新规则前先用
 * `python tools/mt_term_eval.py <模型目录> --backend onnx` 复现一条错译。
 *
 * 以 classic script 与 CommonJS 双形态交付：浏览器里挂到
 * `window.EchoMTRepair`，node 测试里 `import` 后从 `globalThis` 取。
 */

(function (root) {
  'use strict';

  /**
   * @typedef {Object} Rule
   * @property {RegExp} en    英文原文里认这个术语
   * @property {string} zh    正确的中文术语
   * @property {string[]} [wrong] 实测出现过的错译（整串匹配）
   * @property {RegExp} [pattern] 没有整串错译时，用正则就地替换（只换第一处）
   */

  /** @type {Rule[]} */
  const RULES = [
    {
      // ring→戒指、light→灯/光，逐词素直译
      en: /\bring\s+lights?\b/i,
      zh: '环形灯',
      wrong: ['戒指灯', '戒指光', '圆环灯', '环光灯'],
    },
    {
      // telecentric 被切成 tele + centric，两个词素都译成了错的意思
      // 注意是 lens(es)?：写成 lenses? 只能匹配「lense」，单数 lens 直接漏掉
      en: /\btelecentric\s+lens(es)?\b/i,
      zh: '远心镜头',
      wrong: ['远程中心镜头', '远程中枢镜头', '远程镜头', '远摄镜头'],
    },
    {
      // exposure 在非摄影语境里被译成「接触」
      en: /\bexposure\s+time\b/i,
      zh: '曝光时间',
      wrong: ['接触时间', '暴露时间', '暴光时间'],
    },
    {
      // depth of field 的 field 被丢掉，只剩「深度」，句子还通，最容易被漏掉
      en: /\bdepth\s+of\s+field\b/i,
      zh: '景深',
      pattern: /(?<!景)深度/,
    },
  ];

  /**
   * 修一句译文。
   *
   * @param {string} source          英文原文（item.text）
   * @param {string} translation     模型给出的中文译文
   * @returns {string} 修好（或原样）的译文
   */
  function repair(source, translation) {
    const text = String(translation || '');
    if (!text) return translation;
    const original = String(source || '');
    if (!original) return translation;

    let out = text;
    for (const rule of RULES) {
      if (!rule.en.test(original)) continue; // ② 原文没提这个术语，绝不动手
      if (out.indexOf(rule.zh) !== -1) continue; // ③ 已经对了
      let touched = false;
      if (rule.wrong) {
        for (const bad of rule.wrong) {
          if (out.indexOf(bad) === -1) continue;
          out = out.replace(bad, rule.zh);
          touched = true;
          break;
        }
      }
      if (!touched && rule.pattern) {
        // 只换第一处：一句话里出现两次「深度」通常第二次是别的东西
        out = out.replace(rule.pattern, rule.zh);
      }
    }
    return out;
  }

  root.EchoMTRepair = { repair, rules: RULES.length };
})(typeof globalThis !== 'undefined' ? globalThis : this);
