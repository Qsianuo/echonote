package com.echonote.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.io.FileOutputStream
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/**
 * 模型包解压与校验（MtArchive）的 JVM 测试。
 *
 * 这些用例刻意不碰 Android API：zip slip、半截条目、目录层级错位三种问题
 * 在设备上的表现都是"装完了但翻译说模型缺失"，只有在这里才看得清。
 */
class MtArchiveTest {

    private fun tempDir(): File {
        val dir = File.createTempFile("echonote-mt", "")
        dir.delete()
        dir.mkdirs()
        return dir
    }

    private fun zip(entries: Map<String, String>): File {
        val file = File.createTempFile("echonote-mt", ".zip")
        ZipOutputStream(FileOutputStream(file)).use { out ->
            for ((name, body) in entries) {
                out.putNextEntry(ZipEntry(name))
                out.write(body.toByteArray())
                out.closeEntry()
            }
        }
        return file
    }

    private fun modelZip(direction: String = "zh-en"): File {
        val entries = LinkedHashMap<String, String>()
        for (name in MtArchive.MODEL_FILES) entries["mt-$direction/$name"] = "x"
        entries["mt-$direction/README.txt"] = "readme"
        return zip(entries)
    }

    @Test
    fun rejectsTraversalAndAbsoluteEntries() {
        for (bad in listOf("../evil", "../../a/b", "/data/local/tmp/x", "a/../../b", "C:/x", "")) {
            assertFalse("应拒绝：$bad", MtArchive.isSafeEntry(bad))
        }
        assertTrue(MtArchive.isSafeEntry("mt-zh-en/encoder-int8.onnx"))
        assertTrue(MtArchive.isSafeEntry("README.txt"))
    }

    @Test
    fun extractsNestedFilesWithProgress() {
        val dest = tempDir()
        val seen = mutableListOf<Int>()
        MtArchive.extract(modelZip(), dest, onEntry = { entries, _ -> seen.add(entries) })

        assertTrue(File(dest, "mt-zh-en/decoder-past-int8.onnx").isFile)
        assertEquals("readme", File(dest, "mt-zh-en/README.txt").readText())
        assertEquals(MtArchive.MODEL_FILES.size + 1, seen.size)
        assertEquals(seen.sorted(), seen) // 单调递增
    }

    @Test
    fun traversalZipIsRefusedAndWritesNothingOutside() {
        val dest = tempDir()
        val evil = zip(mapOf("../escaped.txt" to "boom"))
        try {
            MtArchive.extract(evil, dest)
            throw AssertionError("应当拒绝路径穿越")
        } catch (expected: IllegalStateException) {
            // 正常路径：入口就拒，dest 之外不会出现文件
        }
        assertFalse(File(dest.parentFile, "escaped.txt").exists())
        assertEquals(0, dest.listFiles()?.size ?: 0)
    }

    @Test
    fun cancellationAbortsExtraction() {
        val dest = tempDir()
        try {
            MtArchive.extract(modelZip(), dest, cancelled = { true })
            throw AssertionError("应当取消")
        } catch (expected: InstallCancelled) {
            // 取消是正常流程，不是错误
        }
    }

    @Test
    fun findsModelDirAtRootNestedOrLoose() {
        val root = tempDir()
        val nested = File(root, "mt-zh-en")
        nested.mkdirs()
        for (name in MtArchive.MODEL_FILES) File(nested, name).writeText("x")
        assertEquals(nested.canonicalPath, MtArchive.findModelDir(root, "zh-en")?.canonicalPath)

        // 平铺（历史包）：模型文件直接躺在解压根目录
        val flat = tempDir()
        for (name in MtArchive.MODEL_FILES) File(flat, name).writeText("x")
        assertEquals(flat.canonicalPath, MtArchive.findModelDir(flat, "zh-en")?.canonicalPath)

        // 缺文件就不算装好
        val broken = tempDir()
        File(broken, "mt-zh-en").mkdirs()
        File(broken, "mt-zh-en/encoder-int8.onnx").writeText("x")
        assertNull(MtArchive.findModelDir(broken, "zh-en"))
    }

    @Test
    fun parsesSha256FileFormats() {
        val digest = "9d595d2ab62d7c28adf48a1a8fe226283c647a5d402f7e6542b503da91e10022"
        assertEquals(digest, MtArchive.parseSha256("$digest  mt-zh-en-int8.zip\n"))
        assertEquals(digest, MtArchive.parseSha256(digest.uppercase()))
        assertEquals(digest, MtArchive.parseSha256("  $digest"))
        assertNull(MtArchive.parseSha256("404: Not Found"))
    }

    @Test
    fun sha256MatchesKnownVector() {
        val file = File.createTempFile("echonote-sha", ".txt")
        file.writeText("abc")
        assertEquals(
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            MtArchive.sha256(file),
        )
        assertNotNull(MtArchive.parseSha256(MtArchive.sha256(file)))
    }
}
