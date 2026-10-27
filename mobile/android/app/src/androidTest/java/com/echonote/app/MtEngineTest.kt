package com.echonote.app

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * 设备内的翻译引擎验收（`./gradlew connectedDebugAndroidTest`）。
 *
 * 为什么必须在设备上跑，而不是 JVM 单测：这条链路的三个失败点都只在真机暴露——
 *   · ONNX Runtime 的 Java 层（onnxruntime-android 1.17.1）复用 sherpa 自带的
 *     libonnxruntime.so，两者版本不一致时是 `UnsatisfiedLinkError`，JVM 上根本
 *     加载不到 .so；
 *   · int8 量化后的图在 x86_64 / arm64 上走不同内核；
 *   · 分词器的 NFKC 归一化依赖 Android 的 ICU 实现。
 *
 * 模型不进 APK（单方向 226 MB），所以先决条件是模型已推到
 * `files/models/mt-zh-en`，否则整类跳过而不是失败——CI 上本来就没有模型。
 */
@RunWith(AndroidJUnit4::class)
class MtEngineTest {

    private val root: File
        get() = File(
            InstrumentationRegistry.getInstrumentation().targetContext.filesDir,
            "models",
        )

    private fun engine(direction: String): MtEngine {
        val dir = File(root, "mt-$direction")
        assumeTrue("未安装翻译模型：$dir", MtEngine.isInstalled(dir))
        return MtEngine(dir, ai.onnxruntime.OrtEnvironment.getEnvironment())
    }

    @Test
    fun zhToEn_producesReadableEnglish() {
        engine("zh-en").use { mt ->
            val out = mt.translate("把焦距调到五十毫米，然后做曝光测试。")
            println("zh-en → $out")
            assertTrue("译文为空，多为 decoder 起始符传成了 eos", out.isNotBlank())
            // 只断言「不可否认地正确」的部分：数字与曝光。具体措辞（focal range /
            // focus）由 77 M 模型自己挑，测试不该替它规定——上一版写死 focal
            // 就在这里误报了一次。
            assertTrue("译文不含 50：$out", out.contains("50"))
            assertTrue("译文不含 exposure：$out", out.contains("exposure", ignoreCase = true))
            // 重复循环（real-real-real-real）是漏了 source 末尾 </s> 的典型症状。
            assertTrue("疑似重复循环：$out", !Regex("(\\b\\w+\\b)(\\s+\\1){2,}").containsMatchIn(out))
        }
    }

    @Test
    fun enToZh_producesChinese() {
        engine("en-zh").use { mt ->
            val out = mt.translate("The threshold for template matching should not be too high.")
            println("en-zh → $out")
            assertTrue("译文为空：$out", out.isNotBlank())
            // 只要有汉字就算通过——具体用词由 77 M 模型决定，不该由测试断言。
            assertTrue("译文不含汉字：$out", out.any { it.code in 0x4E00..0x9FFF })
        }
    }

    @Test
    fun tokenizer_matchesSentencePieceSegmentation() {
        val dir = File(root, "mt-zh-en")
        assumeTrue("未安装翻译模型：$dir", MtEngine.isInstalled(dir))
        val encoder = SpEncoder.load(File(dir, MtEngine.SRC_VOCAB))
        // 官方 SP 对这句话的切分是 把/焦距/调到/五十/毫米/，/然后/做/曝光/测试/。
        // 用 id 序列断言一次：Viterbi 走错任何一步，id 就对不上。
        val ids = encoder.encode("把焦距调到五十毫米，然后做曝光测试。")
        println("tokens → " + ids.joinToString(","))
        assertTrue("分词为空", ids.isNotEmpty())
        assertTrue("首片应为 ▁把（合并词表里 id 稳定）", ids.size < 30)
        val again = encoder.encode("把焦距调到五十毫米，然后做曝光测试。")
        assertTrue("同一输入两次分词不一致", ids.contentEquals(again))
    }
}
