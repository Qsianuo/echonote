/**
 * EchoNote 移动端识别引擎：采集帧进，协议消息出。
 *
 * 这是 app/main.py 的 StreamingSession 在手机上的极简对应物——同样的
 * 分层（VAD 切句 → 流式字幕 → 整句定稿），同样的消息形状（partial /
 * final / refine / warning / done），让上层 UI（app.js）不用关心底下是
 * Python 服务还是原生插件。
 *
 * 差异（有意为之）：
 *  · 解码跨 Capacitor 桥（base64），因此流式解码按“新增音频步长”节流
 *    （LIVE_STEP_S=0.6s，与服务端一致），不做逐帧解码；
 *  · 定稿永远用 SenseVoice（离线、带标点）；流式文本只做字幕与兜底。
 */

import { correct as fixTerms, applyFixes } from './glossary.js';
import { Segmenter, MODES } from './native-vad.js?v=08f18022ac';
import { sherpa } from './native-sherpa.js?v=08f18022ac';
import { mt } from './native-translate.js?v=08f18022ac';

const LIVE_STEP_S = 0.6;
const MIN_FIRST_DECODE_S = 2.0;

export class MobileEngine {
  constructor({ onPartial, onFinal, onRefine, onWarning } = {}) {
    this.handlers = { onPartial, onFinal, onRefine, onWarning };
    this.segmenter = null;
    this.mode = 'personal';
    this.epoch = 0;
    // 译文语向：'' = 不翻译。切换时递增 seq，迟到的旧译文不会贴到新档位上。
    this.translateTo = '';
    this.translateSeq = 0;
    this.reset();
  }

  /** 设置译文语向（来自 config 帧的 translate_to）。 */
  setTranslateTo(target) {
    const next = String(target || '');
    if (next === this.translateTo) return;
    this.translateTo = next;
    this.translateSeq += 1;
  }

  reset() {
    this.utterance = null; // { audio, coveredS, fedSamples, text, decoding }
    this.finalSeq = 0;
    this.lastFinalText = '';
  }

  setMode(mode) {
    if (!MODES[mode]) return;
    this.mode = mode;
  }

  /** 会话开始：建一个新的切句器（情境预设随之生效），epoch 递增。 */
  beginSession() {
    this.segmenter = new Segmenter(MODES[this.mode] || {});
    this.epoch += 1;
    this.reset();
  }

  /** 结束会话（页面卸载 / 壳关闭）。 */
  endSession() {
    this.flushUtterance();
    this.segmenter = null;
  }

  /** 冲一句话尾（暂停 / 停止时调用）：把说到一半的句子立即定稿。 */
  flushUtterance() {
    const tail = this.segmenter && this.segmenter.flush();
    if (tail) this._complete(tail);
    this.utterance = null;
  }

  /** 喂入 16 kHz mono Float32（100 ms 帧与现有采集链路一致）。 */
  push(samples) {
    if (!this.segmenter) return;
    for (const event of this.segmenter.push(samples)) {
      this._complete(event);
    }
    const seg = this.segmenter;
    if (seg.inSpeech) {
      if (!this.utterance) {
        const seed = seg.currentAudio();
        if (seed) {
          // fedSamples=0：第一次流式解码必须从句首（含前滚）喂起，
          // reset=true 建新流；之后只发增量——原生流是累积的，重发全量
          // 会把同一句话叠两遍。
          this.utterance = {
            audio: seed,
            coveredS: 0,
            fedSamples: 0,
            text: '',
            decoding: false,
          };
        }
      } else {
        this.utterance.audio = concat(this.utterance.audio, samples);
        this._maybeLive();
      }
    } else if (this.utterance) {
      this.utterance = null; // 段落已被 _complete 收走/丢弃
    }
  }

  // ----------------------------------------------------------------- live
  async _maybeLive() {
    const u = this.utterance;
    if (!u || u.decoding) return;
    const duration = u.audio.length / 16000;
    if (duration < Math.max(MIN_FIRST_DECODE_S, u.coveredS + LIVE_STEP_S)) return;
    u.decoding = true;
    try {
      const isNewStream = u.fedSamples === 0;
      const delta = u.audio.subarray(u.fedSamples);
      const text = await sherpa.decodeStreaming(delta, { reset: isNewStream });
      u.fedSamples = u.audio.length;
      u.coveredS = duration;
      if (text && text !== u.text) {
        u.text = text;
        this.handlers.onPartial &&
          this.handlers.onPartial({
            text,
            epoch: this.epoch,
            start: this.segmenter.segmentStartS,
          });
      }
    } catch {
      /* 字幕尽力而为：桥或模型未就绪时静默跳过，定稿仍会尝试 */
    } finally {
      u.decoding = false;
    }
  }

  // ---------------------------------------------------------------- final
  async _complete(event) {
    const { audio, start, end } = event;
    const provisional = this.utterance ? this.utterance.text : '';
    this.utterance = null;

    if (end - start < 1.5 && sameLine(provisional, this.lastFinalText)) {
      // 与服务端同样的短段重复守卫：房间噪声切成 ~1s 的小段、每段解码出
      // 同一句，读起来像卡住——第二段直接丢弃。
      return;
    }

    let text = provisional;
    try {
      text = (await sherpa.decodeFinal(audio)) || provisional;
    } catch {
      /* 离线模型未就绪时保留流式文本 */
    }
    text = (text || '').trim();
    if (!text) return;

    // 术语纠错只作用于定稿，格式与桌面端一致（app/main.py 的 _decode_final）：
    // 实时字幕边出边长，在半句上按读音纠错会把还在变的词判错。桌面的术语表
    // 可由用户编辑，移动端随包固化——改术语表要重跑 tools/gen_glossary_data.py。
    text = fixTerms(text);
    // 非同音的整词听错（魔方匹配 → 模板匹配）走另一张表，与桌面端同源。
    text = applyFixes(text);

    this.finalSeq += 1;
    this.lastFinalText = text;
    const payload = { id: `${this.epoch}-${this.finalSeq}`, epoch: this.epoch, text, start, end };
    this.handlers.onFinal && this.handlers.onFinal({ ...payload, type: 'final', stable: false });
    this.handlers.onRefine && this.handlers.onRefine({ ...payload, type: 'refine' });

    // 译文后补，不挡出字：手机上翻一句要几百毫秒到两秒，若等它算完再发
    // final，用户盯着空白等字幕——原文先落地，译文算完再以同 id 的 refine
    // 补上（app.js 的 onRefine 会更新译文而不动位置）。
    this._translateInto(payload);
  }

  async _translateInto(payload) {
    const target = this.translateTo;
    if (!target) return;
    const seq = this.translateSeq;
    const text = await mt.translate(payload.text, target);
    if (!text || seq !== this.translateSeq) return; // 语向已切换，旧译文作废
    this.handlers.onRefine &&
      this.handlers.onRefine({ ...payload, type: 'refine', translation: text });
  }
}

function concat(a, b) {
  const out = new Float32Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

/** 与 stt.same_line 相同的形状比较：忽略标点，短的完全包含于长的即视为同句。 */
function sameLine(a, b) {
  const core = (t) => (t || '').replace(/[\s\W_]+/gu, '');
  const ca = core(a);
  const cb = core(b);
  if (!ca || !cb) return false;
  if (ca === cb) return true;
  const [short, long] = ca.length <= cb.length ? [ca, cb] : [cb, ca];
  return long.replace(short, '') === '';
}
