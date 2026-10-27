package com.echonote.app

import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 一份 ORT 供两个引擎共用——这个决定必须被盯着。
 *
 * 背景：sherpa-onnx 自带一份 ``libonnxruntime.so``，翻译用的
 * ``onnxruntime-android`` 也带一份，两者**同名**，在同一个进程里只能留一份。
 * 最初的做法是「复用 sherpa 那份，只加 Java 层」，运行时报
 * ``cannot locate symbol "OrtGetApiBase"``——那份是定制构建，符号对不上。
 *
 * 现在的做法：整份换成官方 1.17.1（它多导出的一组符号 sherpa 用不到，
 * 而 sherpa 只需要 ``OrtGetApiBase``，见静态符号核对）。这个测试就是
 * 保证「换 ORT 之后 sherpa 还能加载」——只验链接，不验识别（模型不在 APK 里）。
 */
@RunWith(AndroidJUnit4::class)
class OrtCompatTest {

    @Test
    fun sherpaJniLoadsAgainstSharedOrt() {
        System.loadLibrary("sherpa-onnx-jni")
        // 能走到这里说明 dlopen 成功、JNI_OnLoad 返回正常、对 libonnxruntime.so
        // 的未定义符号全部解析到位。失败会是 UnsatisfiedLinkError。
        assertTrue(true)
    }

    @Test
    fun ortJavaLayerLoads() {
        ai.onnxruntime.OrtEnvironment.getEnvironment()
        val providers = ai.onnxruntime.OrtEnvironment.getAvailableProviders()
        assertTrue("OrtEnvironment 未初始化", providers.isNotEmpty())
        println("ORT providers: $providers")
    }
}
