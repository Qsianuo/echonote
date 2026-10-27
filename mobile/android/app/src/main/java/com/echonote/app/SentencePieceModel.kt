package com.echonote.app

import java.io.File
import java.text.Normalizer

/**
 * SentencePiece unigram 分词（纯 Kotlin，不依赖 sentencepiece 原生库）。
 *
 * 为什么自己写：Marian 模型的输入不是「字」，是 SP 片段；而 Android 上没有
 * 现成的 sentencepiece JNI，为一个分词器引入 NDK 构建不值得。unigram 的编码
 * 就是一遍 Viterbi——取「路径总得分最高」的切法，词表与分数由
 * tools/export_mt_vocab.py 在桌面侧导出成文本，这里只读文本。
 *
 * **两份词表是必须的**：ONNX 里的 id 空间是 Marian 的**合并词表**（source 的
 * 32000 片段 + target 的 32000 片段，去重后 65000），而 .spm 里的 id 是 0..31999。
 * 所以编码表把「片段 → 合并 id → 分数」一次性算好，解码表则是「id → 片段」。
 *
 * 归一化：SP 的 nmt_nfkc 近似 = NFKC + 空白规范化 + 每个空格换成 ▁ + 句首补 ▁。
 */

/** 编码方向：文本 → 合并词表 id。 */
class SpEncoder private constructor(
    private val pieceToId: Map<String, Int>,
    private val pieceScore: Map<String, Float>,
    private val unkId: Int,
    private val maxPieceLen: Int,
    private val minScore: Float,
) {
    companion object {
        private const val MARK = '\u2581' // ▁
        private const val NEG = -1.0e30f

        /**
         * 读 ``片段<TAB>id<TAB>分数`` 文本。文件首行的 ``# eos=.. unk=.. pad=..``
         * 是导出脚本写的特殊 id；缺失时退回 Marian 的惯例值。
         */
        fun load(file: File): SpEncoder {
            val pieceToId = HashMap<String, Int>(40000)
            val pieceScore = HashMap<String, Float>(40000)
            var unkId = 1
            var maxLen = 1
            var minScore = 0f
            var first = true

            file.forEachLine { raw ->
                val line = raw.trimEnd('\n', '\r')
                if (line.isEmpty()) return@forEachLine
                if (line.startsWith("#")) {
                    if (first) {
                        Regex("unk=(\\d+)").find(line)?.let { unkId = it.groupValues[1].toInt() }
                    }
                    return@forEachLine
                }
                first = false
                val parts = line.split('\t')
                if (parts.size < 3) return@forEachLine
                val piece = parts[0]
                val id = parts[1].toIntOrNull() ?: return@forEachLine
                val score = parts[2].toFloatOrNull() ?: 0f
                pieceToId[piece] = id
                pieceScore[piece] = score
                if (piece.length > maxLen) maxLen = piece.length
                if (score < minScore) minScore = score
            }
            // 片段最长不过几十个字符；截到 24 纯粹是给内层循环一个上界。
            return SpEncoder(pieceToId, pieceScore, unkId, minOf(maxLen, 24), minScore)
        }
    }

    /** 文本 → 合并词表 id（不含句尾 </s>，调用方自己补）。 */
    fun encode(text: String): IntArray {
        val s = normalize(text)
        val n = s.length
        if (n == 0) return IntArray(0)

        val best = FloatArray(n + 1) { NEG }
        val prev = IntArray(n + 1) { -1 }
        val stepId = IntArray(n + 1)
        best[0] = 0f

        for (i in 0 until n) {
            if (best[i] == NEG) continue
            var len = 1
            var single = false
            while (len <= maxPieceLen && i + len <= n) {
                val piece = s.substring(i, i + len)
                val id = pieceToId[piece]
                if (id != null) {
                    if (len == 1) single = true
                    val score = best[i] + (pieceScore[piece] ?: 0f)
                    if (score > best[i + len]) {
                        best[i + len] = score
                        prev[i + len] = i
                        stepId[i + len] = id
                    }
                }
                len += 1
            }
            // 单字符都不在词表里 → 走 unk 边（未登录字/罕见符号）。
            if (!single) {
                val score = best[i] + minScore - 10f
                if (score > best[i + 1]) {
                    best[i + 1] = score
                    prev[i + 1] = i
                    stepId[i + 1] = unkId
                }
            }
        }

        // 回溯：从结尾往前走，边权就是该切片的 id。
        var cursor = n
        var count = 0
        while (cursor > 0 && prev[cursor] >= 0) {
            count += 1
            cursor = prev[cursor]
        }
        if (cursor != 0) return IntArray(0) // 有字符没被切到（理论上不会发生）
        val out = IntArray(count)
        cursor = n
        var at = count - 1
        while (cursor > 0 && at >= 0) {
            out[at] = stepId[cursor]
            at -= 1
            cursor = prev[cursor]
        }
        return out
    }

    /** nmt_nfkc 的近似：NFKC → 空白收敛 → 空格换 ▁ → 句首补 ▁。 */
    private fun normalize(text: String): String {
        val normalized = Normalizer.normalize(text, Normalizer.Form.NFKC)
            .replace('\u00A0', ' ')
            .trim()
        if (normalized.isEmpty()) return ""
        val sb = StringBuilder(normalized.length + 1)
        sb.append(MARK)
        for (ch in normalized) {
            sb.append(if (ch == ' ' || ch == '\t' || ch == '\n') MARK else ch)
        }
        return sb.toString()
    }
}

/** 解码方向：合并词表 id → 文本。 */
class SpDecoder private constructor(
    private val idToPiece: Map<Int, String>,
    private val eosId: Int,
) {
    companion object {
        private const val MARK = '\u2581'

        fun load(file: File): SpDecoder {
            val idToPiece = HashMap<Int, String>(40000)
            var eosId = 0
            var first = true
            file.forEachLine { raw ->
                val line = raw.trimEnd('\n', '\r')
                if (line.isEmpty()) return@forEachLine
                if (line.startsWith("#")) {
                    if (first) {
                        Regex("eos=(\\d+)").find(line)?.let { eosId = it.groupValues[1].toInt() }
                    }
                    return@forEachLine
                }
                first = false
                val tab = line.indexOf('\t')
                if (tab <= 0) return@forEachLine
                val id = line.substring(0, tab).toIntOrNull() ?: return@forEachLine
                idToPiece[id] = line.substring(tab + 1)
            }
            return SpDecoder(idToPiece, eosId)
        }
    }

    /** id 序列 → 自然文本（遇 </s> 停，▁ 还原成空格，收拢标点前多余空格）。 */
    fun decode(ids: IntArray): String {
        val sb = StringBuilder()
        var atWordStart = true
        for (id in ids) {
            if (id == eosId) break
            var piece = idToPiece[id] ?: continue
            if (piece.startsWith(MARK)) {
                piece = piece.substring(1)
                if (!atWordStart) sb.append(' ')
            }
            if (piece.isEmpty()) continue
            sb.append(piece)
            atWordStart = false
        }
        return tidy(sb.toString())
    }

    /** Marian 输出的英文常带「词 空格 标点」，SP 解码后要收掉。 */
    private fun tidy(text: String): String {
        var s = text.trim()
        s = s.replace(Regex("[ \\t]+([,.!?;:%)])"), "$1")
        s = s.replace(Regex("([(])[ \\t]+"), "$1")
        s = s.replace(Regex("[ \\t]{2,}"), " ")
        return s.trim()
    }
}
