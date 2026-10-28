package com.echonote.app

import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest
import java.util.zip.ZipInputStream

/** 用户主动取消（与真正的错误区分开：界面不该弹红字）。 */
internal class InstallCancelled : Exception("已取消")

/**
 * 翻译模型包（zip）的校验与解压。
 *
 * 为什么单独一个文件：这些逻辑不碰 Android API，能在 JVM 单元测试里直接跑
 * （`app/src/test`），不必连真机。路径穿越、半截文件、目录层级这三种问题
 * 各有一条测试锁着——它们出错时的表现都是"装完了但翻译报模型缺失"，
 * 在设备上极难查。
 */
internal object MtArchive {

    /** 一个方向只有 5 个真文件 + README，条目数给足余量但不放任。 */
    private const val MAX_ENTRIES = 64

    /** 解压总量上限：正常 237 MB；超过 2 GB 只可能是炸弹或坏包。 */
    private const val MAX_TOTAL_BYTES = 2L * 1024 * 1024 * 1024

    /** 解压后判定"装好了"所需的最小文件集，与 MtEngine.isInstalled 同源。 */
    val MODEL_FILES = listOf(
        "encoder-int8.onnx",
        "decoder-int8.onnx",
        "decoder-past-int8.onnx",
        "mt-src-vocab.txt",
        "mt-tgt-vocab.txt",
    )

    /**
     * 条目名是否可以安全落盘。
     *
     * zip 里的名字是**外部输入**：`../../databases/x` 这种条目在解压时会把
     * 文件写到应用沙盒之外（zip slip）。判据取保守的一侧：绝对路径、任何
     * 一段 `..`、盘符、空名，一律拒。
     */
    fun isSafeEntry(name: String): Boolean {
        if (name.isBlank()) return false
        if (name.startsWith("/") || name.startsWith("\\")) return false
        if (name.contains(":")) return false
        return name.split('/', '\\').none { it == ".." }
    }

    /**
     * 解压到 [dest]。每写完一个文件回调一次（用于进度），[cancelled] 为真时
     * 抛 [InstallCancelled]。失败时**不清理** dest——清理由调用方统一做，
     * 这里只保证不会写到 dest 之外。
     */
    fun extract(
        zip: File,
        dest: File,
        onEntry: (entries: Int, bytes: Long) -> Unit = { _, _ -> },
        cancelled: () -> Boolean = { false },
    ) {
        if (!dest.isDirectory && !dest.mkdirs()) {
            throw IllegalStateException("无法创建解压目录 ${dest.absolutePath}")
        }
        val root = dest.canonicalFile
        val rootPrefix = root.path + File.separator

        var entries = 0
        var bytes = 0L
        ZipInputStream(zip.inputStream().buffered()).use { zin ->
            while (true) {
                if (cancelled()) throw InstallCancelled()
                val entry = zin.nextEntry ?: break
                if (!isSafeEntry(entry.name)) {
                    throw IllegalStateException("模型包含非法路径：${entry.name}")
                }
                if (entry.isDirectory) continue

                entries++
                if (entries > MAX_ENTRIES) throw IllegalStateException("模型包条目异常（>$MAX_ENTRIES）")

                val out = File(root, entry.name)
                // 名字已经过一遍检查，这里再用 canonicalPath 兜底：
                // Windows 上反斜杠、大小写、符号链接都可能绕过字符串判断。
                if (!out.canonicalPath.startsWith(rootPrefix)) {
                    throw IllegalStateException("模型包路径越界：${entry.name}")
                }
                out.parentFile?.mkdirs()
                FileOutputStream(out).use { os ->
                    val buf = ByteArray(256 * 1024)
                    while (true) {
                        if (cancelled()) throw InstallCancelled()
                        val n = zin.read(buf)
                        if (n < 0) break
                        os.write(buf, 0, n)
                        bytes += n
                        if (bytes > MAX_TOTAL_BYTES) {
                            throw IllegalStateException("模型包解压后过大，已中止")
                        }
                    }
                }
                onEntry(entries, bytes)
            }
        }
    }

    /**
     * 在解压目录里找到真正的模型目录。
     *
     * 打包脚本把文件放在 `mt-zh-en/` 下，但历史上有过直接平铺的包；两种都认，
     * 再退一步只认"能通过 isInstalled 判据的目录"——比写死层级更耐用。
     */
    fun findModelDir(root: File, direction: String): File? {
        fun installed(dir: File): Boolean =
            MODEL_FILES.all { name -> File(dir, name).let { it.isFile && it.length() > 0 } }

        if (installed(root)) return root
        val named = File(root, "mt-$direction")
        if (installed(named)) return named
        return root.listFiles()
            ?.filter { it.isDirectory }
            ?.firstOrNull { installed(it) }
    }

    /** 整文件 SHA-256（小写 hex）。154 MB 在手机上约 0.3–0.6 s。 */
    fun sha256(file: File): String {
        val md = MessageDigest.getInstance("SHA-256")
        file.inputStream().buffered(256 * 1024).use { input ->
            val buf = ByteArray(256 * 1024)
            while (true) {
                val n = input.read(buf)
                if (n < 0) break
                md.update(buf, 0, n)
            }
        }
        return md.digest().joinToString("") { "%02x".format(it.toInt() and 0xFF) }
    }

    /**
     * 从 `.sha256` 文件里取摘要。该文件的格式是 `sha256sum` 的默认输出：
     * `<64 位 hex>  <文件名>`，也可能只有摘要本身。
     */
    fun parseSha256(text: String): String? =
        Regex("[0-9a-fA-F]{64}").find(text)?.value?.lowercase()
}
