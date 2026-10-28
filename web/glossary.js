/**
 * 同音纠错（移动端实现）：把定稿文本里与术语同音的片段换回术语本身。
 *
 * 桌面端这一步跑在 Python 里（app/glossary.py），原生壳没有 Python，所以
 * 在这里重写一份，判据完全照抄：
 *
 *  1. 窗口必须全是汉字（否则「，脚距」会连标点一起被当成多一个音节吞掉）；
 *  2. 等长同音永远放行（脚距 → 焦距）；
 *  3. 比术语多一个音节只对 **3 字以上** 术语放行（模板马匹配 → 模板匹配）——
 *     2 字术语太容易吃掉邻居（「先做标定」的「做」曾把整段拼成「标定」）；
 *  4. 长术语优先，避免「标定板」被「标定」先截走。
 *
 * 与 Python 版的唯一差异：Python 用 lazy_pinyin 对**整段**取音，享有词级
 * 上下文（「曝光」整段读 bao guang）；JS 没有词库，只能逐字查多音字表，
 * 用「读音集合相交」代替相等。因此「暴光」仍然能对上「曝光」（曝 有 bao
 * 音），代价是理论上比 Python 版略宽松——故本文件只做**同音替换**，达不到
 * 同音就一个字都不动。
 *
 * 术语与拼音数据由 tools/gen_glossary_data.py 从 models/glossary.txt 生成，
 * 改术语表后重跑该脚本（桌面端不受影响，它读的是同一份 .txt）。
 */

import { PINYIN, TERMS, TERM_FIXES } from './glossary-data.js';

const HAN = /^[\u4e00-\u9fa5]$/;

/** char → 全部无调读音。数据是「音节:同音字」的行文本，首次调用时展开。 */
let charReadings = null;

/** 预处理后的术语表，长词在前。*/
let termTable = null;

function ensureIndex() {
  if (charReadings) return;
  charReadings = new Map();
  for (const line of PINYIN.split('\n')) {
    const at = line.indexOf(':');
    if (at <= 0) continue;
    const syllable = line.slice(0, at);
    for (const ch of line.slice(at + 1)) {
      const bucket = charReadings.get(ch);
      if (bucket) bucket.push(syllable);
      else charReadings.set(ch, [syllable]);
    }
  }

  termTable = [];
  for (const term of TERMS) {
    const readings = readingsOf(term);
    if (readings && readings.length >= 2) termTable.push({ term, readings });
  }
  // TERMS 已按长→短生成，这里再稳一次，防止上游排序变化。
  termTable.sort((a, b) => b.term.length - a.term.length);
}

/** 文本的逐字读音；非汉字位置为 null。整词有非汉字时返回 null。 */
function readingsOf(text) {
  const out = [];
  for (const ch of text) {
    if (!HAN.test(ch)) return null;
    const variants = charReadings.get(ch);
    if (!variants) return null;
    out.push(variants);
  }
  return out;
}

function sharesSyllable(a, b) {
  for (const x of a) if (b.includes(x)) return true;
  return false;
}

/** 窗口读音是否可视为术语的同音写法（逐位相交即算同音）。 */
function sameSound(window, term) {
  for (let i = 0; i < term.length; i += 1) {
    if (!sharesSyllable(window[i], term[i])) return false;
  }
  return true;
}

/** 多一个音节的对齐：删掉窗口里任意一个字后是否同音。 */
function sameSoundDroppingOne(window, term) {
  if (window.length !== term.length + 1) return false;
  for (let skip = 0; skip < window.length; skip += 1) {
    const head = window.slice(0, skip);
    const tail = window.slice(skip + 1);
    if (sameSound(head.concat(tail), term)) return true;
  }
  return false;
}

/**
 * 纠正一段文本。``terms`` 可传入自定义术语表（测试用），默认用打包数据。
 * 反复调用是安全的：已经正确的术语会原样保留。
 */
export function correct(text, terms = null) {
  if (!text) return text;
  ensureIndex();

  const table = terms ? buildTable(terms) : termTable;
  if (!table.length) return text;

  const chars = [...text];
  // 逐字读音缓存：标点位置是 null，越界也算 null。
  const readings = chars.map((ch) => {
    if (!HAN.test(ch)) return null;
    return charReadings.get(ch) || null;
  });

  const out = [];
  let i = 0;
  const n = chars.length;
  while (i < n) {
    let hitWidth = 0;
    let hitTerm = null;
    for (const entry of table) {
      const size = entry.term.length;
      for (const width of [size, size + 1, size - 1]) {
        // 语音上少一个音节不纠（与 Python 版一致：宁可少纠，不可纠错）。
        if (width < 2 || i + width > n) continue;
        const window = readings.slice(i, i + width);
        if (window.includes(null)) continue;
        const matched =
          width === size
            ? sameSound(window, entry.readings)
            : entry.term.length >= 3 && sameSoundDroppingOne(window, entry.readings);
        if (matched) {
          hitWidth = width;
          hitTerm = entry.term;
          break;
        }
      }
      if (hitTerm) break;
    }
    if (hitTerm) {
      out.push(hitTerm);
      i += hitWidth;
    } else {
      out.push(chars[i]);
      i += 1;
    }
  }
  return out.join('');
}

/** 自定义术语表 → 与 termTable 同形状（长词优先）。 */
function buildTable(terms) {
  const table = [];
  for (const raw of terms) {
    const term = (raw || '').trim();
    if (term.length < 2) continue;
    const readings = readingsOf(term);
    if (readings) table.push({ term, readings });
  }
  table.sort((a, b) => b.term.length - a.term.length);
  return table;
}

/** 术语总数（UI 上「已启用 N 个术语」之类文案用得到）。 */
export function termCount() {
  ensureIndex();
  return termTable.length;
}


/**
 * 非同音错词表的整串替换（桌面端对应 app/term_fixes.py）。
 *
 * 读音纠错救不了「魔方匹配 → 模板匹配」这种整词听错——两者读音完全不同。
 * 这里只认表里写明的串，长串优先，不猜、不做模糊匹配：这一层没有词边界，
 * 一放宽就必然误伤。
 */
export function applyFixes(text, rules = null) {
  if (!text) return text;
  const table = rules || TERM_FIXES || [];
  let out = text;
  for (const [wrong, right] of table) {
    if (wrong && out.includes(wrong)) out = out.split(wrong).join(right);
  }
  return out;
}
