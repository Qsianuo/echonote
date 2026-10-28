package com.echonote.app

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import com.getcapacitor.JSArray
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.nio.FloatBuffer
import java.nio.LongBuffer
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors

/**
 * EchoNote 离线翻译插件（Android）。
 *
 * 与桌面端 app/translate.py 是同一套模型（Opus-MT 77M），只是换了执行环境：
 * 桌面走 ctranslate2，这里走 ONNX Runtime。两端输出应当接近到"读起来一样"，
 * 实现上却是两套代码，所以模型名、方向、语种标签必须严格对齐：
 *
 *   target="en" → mt-zh-en（源中文）    target="zh" → mt-en-zh（源英文）
 *
 * 三个 ONNX 会话对应 Marian 的三段执行：encoder 一次，decoder 出第一步
 * （要算 cross-attention 的 K/V），decoder_with_past 之后每步只喂上一个
 * token + 缓存的 K/V——没有 past 缓存的话，一句话要重算 n 次全序列。
 *
 * 分词在 SentencePieceModel.kt（纯 Kotlin 的 unigram Viterbi）。这里只管
 * 张量怎么摆、循环什么时候停。
 */
@CapacitorPlugin(name = "SherpaMT")
class SherpaMTPlugin : Plugin() {

    private val env: OrtEnvironment by lazy { OrtEnvironment.getEnvironment() }

    /** 方向 → 引擎。同一次会话里反复翻译同一方向时不必重建会话（模型加载 ~1 s）。 */
    private val engines = ConcurrentHashMap<String, MtEngine>()

    /** 推理放在单线程池：Capacitor 的 bridge 线程要留给录音与状态推送。 */
    private val worker = Executors.newSingleThreadExecutor()

    /**
     * 探测本机可用的语向。``root`` 是模型根目录（通常是应用沙盒的 models/），
     * 只看模型文件齐不齐——齐了才把自己报上去，界面才不会出现一个点了没反应
     * 的选项。
     */
    @PluginMethod
    fun available(call: PluginCall) {
        val root = modelRoot(call)
        val targets = JSArray()
        // 目录名是 mt-<源>-<目标>，而对外报的是"目标语种"，两者不是同一个东西：
        // target="en" 对应目录 mt-zh-en。这里必须反查，否则会去 mt-en 找模型。
        for ((target, direction) in DIRECTION_BY_TARGET) {
            if (resolveDirectionDir(root, direction) != null) targets.put(target)
        }
        val ret = JSObject()
        ret.put("targets", normalizeTargets(targets))
        call.resolve(ret)
    }

    /**
     * 翻一句。``target`` 是目标语种（"zh" / "en"）；源语种由方向决定，
     * 与桌面端一致——用户把识别语言设成「中文」却说了英文时，文本自己说明
     * 了它是什么语言，不该跟着界面选项走。
     */
    @PluginMethod
    fun translate(call: PluginCall) {
        val text = call.getString("text") ?: return call.reject("缺少 text")
        val target = call.getString("target") ?: return call.reject("缺少 target")
        val root = modelRoot(call)
        val direction = DIRECTION_BY_TARGET[target]
            ?: return call.reject("不支持的译文语种 $target")

        worker.execute {
            try {
                val engine = engines.getOrPut(direction) {
                    val dir = resolveDirectionDir(root, direction)
                        ?: throw IllegalStateException("翻译模型未安装")
                    MtEngine(dir, env)
                }
                val ret = JSObject()
                ret.put("text", engine.translate(text))
                call.resolve(ret)
            } catch (e: Exception) {
                call.reject("翻译失败: ${e.message}")
            }
        }
    }

    // ------------------------------------------------------------------
    // 模型安装（应用内下载，2026-10-28）
    // ------------------------------------------------------------------

    /** 下载/解压线程：与推理线程分开，237 MB 的解压不能卡住正在翻的句子。 */
    private val installer = Executors.newSingleThreadExecutor()

    /** 同一时刻只允许一个方向在装：两路 148 MB 会互相抢带宽与存储。 */
    @Volatile
    private var installing: String? = null

    /** 取消标志，只在当前任务内有效。 */
    @Volatile
    private var cancelRequested = false

    /**
     * 两个方向的安装状态。界面据此决定按钮是「下载」还是「已安装」，
     * 也据此算还要腾多少空间（freeBytes 是安装位置的可用空间）。
     */
    @PluginMethod
    fun modelStatus(call: PluginCall) {
        val root = modelRoot(call)
        val items = JSArray()
        for (direction in TARGET_BY_DIRECTION.keys) {
            val installedDir = resolveDirectionDir(root, direction)
            val obj = JSObject()
            obj.put("direction", direction)
            obj.put("target", TARGET_BY_DIRECTION[direction])
            obj.put("installed", installedDir != null)
            obj.put("bytes", installedDir?.let { dirBytes(it) } ?: 0L)
            obj.put("path", installedDir?.absolutePath ?: "")
            items.put(obj)
        }
        val ret = JSObject()
        ret.put("items", items)
        ret.put("freeBytes", writableRoot(call.getString("root")).usableSpace)
        ret.put("installing", installing ?: "")
        call.resolve(ret)
    }

    /**
     * 下载并安装一个方向。异步执行（148 MB 下载 + 237 MB 解压），过程中用
     * `mtProgress` 事件推进度；完成后 resolve 一次。
     */
    @PluginMethod
    fun downloadModel(call: PluginCall) {
        val direction = call.getString("direction") ?: return call.reject("缺少 direction")
        if (!TARGET_BY_DIRECTION.containsKey(direction)) return call.reject("未知方向 $direction")
        if (installing != null) return call.reject("另一个方向的模型正在安装，请稍候")

        val givenRoot = call.getString("root")
        installing = direction
        cancelRequested = false
        installer.execute {
            try {
                call.resolve(install(direction, givenRoot))
            } catch (e: InstallCancelled) {
                val ret = JSObject()
                ret.put("ok", false)
                ret.put("cancelled", true)
                call.resolve(ret)
            } catch (e: Exception) {
                call.reject("翻译模型安装失败：${e.message}")
            } finally {
                installing = null
                cancelRequested = false
            }
        }
    }

    /** 取消当前安装。下载中立即生效；解压中在下一个读块生效。 */
    @PluginMethod
    fun cancelDownload(call: PluginCall) {
        cancelRequested = true
        val ret = JSObject()
        ret.put("ok", true)
        call.resolve(ret)
    }

    private fun install(direction: String, givenRoot: String?): JSObject {
        val ret = JSObject()
        ret.put("direction", direction)
        val root = writableRoot(givenRoot)
        val target = File(root, "mt-$direction")

        // 已经装好就不要再下 148 MB：内部（旧 adb 布局）与外部都算装好。
        if (MtEngine.isInstalled(target)) return installedResult(ret, target, verified = true)
        resolveDirectionDir(root.absolutePath, direction)?.let { existing ->
            return installedResult(ret, existing, verified = true)
        }

        val work = File(root, ".mt-install")
        if (!work.isDirectory && !work.mkdirs()) throw IllegalStateException("无法创建临时目录")
        val zipName = "mt-$direction-int8.zip"
        val url = RELEASE_BASE + zipName
        val zip = File(work, zipName)
        val staging = File(work, "$direction.part")

        try {
            staging.deleteRecursively()
            zip.delete()

            notifyMt(direction, "resolve")
            val total = headContentLength(url)
            // 需要同时装下 zip（154 MB）与解压结果（237 MB）≈ 2.7 倍；总量未知时按 520 MB 估。
            val needed = if (total > 0) (total * 2.7).toLong() else 520L * 1024 * 1024
            val usable = root.usableSpace
            if (usable in 1 until needed) {
                throw IllegalStateException(
                    "存储空间不足：需要约 ${mb(needed)} MB，可用 ${mb(usable)} MB"
                )
            }

            notifyMt(direction, "download", 0, total)
            download(url, zip, total, direction)

            notifyMt(direction, "verify")
            val verified = verifyChecksum(zip, "$url.sha256")

            notifyMt(direction, "unzip")
            MtArchive.extract(
                zip,
                staging,
                onEntry = { entries, bytes -> notifyMt(direction, "unzip", entries = entries, bytes = bytes) },
                cancelled = { cancelRequested },
            )

            val src = MtArchive.findModelDir(staging, direction)
                ?: throw IllegalStateException("模型包内容不完整（找不到 ${MtArchive.MODEL_FILES.first()}）")
            if (!MtEngine.isInstalled(src)) {
                throw IllegalStateException("解压结果未通过完整性检查")
            }
            if (target.exists() && !target.deleteRecursively()) {
                throw IllegalStateException("无法替换旧模型目录 ${target.absolutePath}")
            }
            if (!src.renameTo(target)) {
                if (!src.copyRecursively(target, overwrite = true)) {
                    throw IllegalStateException("无法写入 ${target.absolutePath}")
                }
                src.deleteRecursively()
            }

            notifyMt(direction, "done")
            return installedResult(ret, target, verified)
        } finally {
            staging.deleteRecursively()
            zip.delete()
            // 临时目录空了就顺手删掉，别在用户存储里留空壳。
            if (work.listFiles()?.isEmpty() == true) work.delete()
        }
    }

    private fun installedResult(ret: JSObject, dir: File, verified: Boolean): JSObject {
        ret.put("ok", true)
        ret.put("installed", true)
        ret.put("bytes", dirBytes(dir))
        ret.put("verified", verified)
        ret.put("path", dir.absolutePath)
        return ret
    }

    /**
     * 下载 zip 到 [target]（先写 `.part`，校验长度后改名）。
     * 进度每 300 ms 上报一次——再密只是白烧桥接带宽。
     */
    private fun download(url: String, target: File, total: Long, direction: String) {
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.connectTimeout = 15000
        conn.readTimeout = 60000
        conn.instanceFollowRedirects = true
        conn.setRequestProperty("User-Agent", UA)
        try {
            val code = conn.responseCode
            if (code != 200) {
                throw IllegalStateException("HTTP $code（该版本可能没有附带模型包）")
            }
            // HEAD 拿不到时用 GET 自己的 Content-Length 兜底。
            val expected = if (total > 0) total else conn.contentLengthLong
            val part = File(target.parentFile, target.name + ".part")
            var received = 0L
            var lastNotify = 0L
            FileOutputStream(part).use { out ->
                conn.inputStream.use { input ->
                    val buf = ByteArray(256 * 1024)
                    while (true) {
                        if (cancelRequested) throw InstallCancelled()
                        val n = input.read(buf)
                        if (n < 0) break
                        out.write(buf, 0, n)
                        received += n
                        val now = System.currentTimeMillis()
                        if (now - lastNotify > 300) {
                            lastNotify = now
                            notifyMt(direction, "download", received, expected)
                        }
                    }
                }
            }
            if (expected > 0 && received != expected) {
                part.delete()
                throw IllegalStateException("下载不完整：$received / $expected 字节")
            }
            if (!part.renameTo(target)) throw IllegalStateException("无法保存到 ${target.absolutePath}")
            notifyMt(direction, "download", received, maxOf(expected, received))
        } finally {
            conn.disconnect()
        }
    }

    /**
     * 用 Release 里同名的 `.sha256` 校验整包。
     *
     * 拿不到摘要（老版本没挂这个资产）时**不拦下载**——HTTPS + zip 结构 + 长度
     * 已经挡住绝大多数问题；但会把 `verified=false` 如实报给界面，不假装校验过。
     */
    private fun verifyChecksum(zip: File, shaUrl: String): Boolean {
        val expected = try {
            MtArchive.parseSha256(fetchText(shaUrl))
        } catch (e: Exception) {
            null
        } ?: return false
        val actual = MtArchive.sha256(zip)
        if (!actual.equals(expected, ignoreCase = true)) {
            throw IllegalStateException("模型包校验失败（下载可能被截断，请重试）")
        }
        return true
    }

    private fun fetchText(url: String): String {
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.connectTimeout = 15000
        conn.readTimeout = 30000
        conn.instanceFollowRedirects = true
        conn.setRequestProperty("User-Agent", UA)
        try {
            if (conn.responseCode != 200) throw IllegalStateException("HTTP ${conn.responseCode}")
            return conn.inputStream.bufferedReader().use { it.readText() }.take(4096)
        } finally {
            conn.disconnect()
        }
    }

    /** HEAD 拿 Content-Length（跟随重定向）；失败返回 -1。 */
    private fun headContentLength(url: String): Long {
        return try {
            val conn = URL(url).openConnection() as HttpURLConnection
            conn.connectTimeout = 15000
            conn.readTimeout = 30000
            conn.requestMethod = "HEAD"
            conn.instanceFollowRedirects = true
            conn.setRequestProperty("User-Agent", UA)
            val len = if (conn.responseCode == 200) conn.contentLengthLong else -1L
            conn.disconnect()
            len
        } catch (e: Exception) {
            -1L
        }
    }

    private fun notifyMt(
        direction: String,
        phase: String,
        received: Long = 0,
        total: Long = 0,
        entries: Int = 0,
        bytes: Long = 0,
    ) {
        val data = JSObject()
        data.put("direction", direction)
        data.put("phase", phase)
        if (total > 0) {
            data.put("received", received)
            data.put("total", total)
        } else if (received > 0) {
            data.put("received", received)
        }
        if (entries > 0) {
            data.put("entries", entries)
            data.put("bytes", bytes)
        }
        notifyListeners("mtProgress", data)
    }

    /**
     * 写入位置：外部应用目录优先（`Android/data/.../files/models`），
     * 拿不到外部目录才退回内部。与 [resolveDirectionDir] 的读取顺序不同是
     * 有意的——读要兼容历史安装，写要选用户能自己清理的位置。
     */
    private fun writableRoot(given: String?): File {
        val dir = if (!given.isNullOrBlank()) {
            File(dirOf(given))
        } else {
            context.getExternalFilesDir(null)?.let { File(it, "models") }
                ?: File(context.filesDir, "models")
        }
        if (!dir.isDirectory && !dir.mkdirs()) {
            throw IllegalStateException("无法创建模型目录 ${dir.absolutePath}")
        }
        return dir
    }

    private fun dirBytes(dir: File): Long =
        dir.walkTopDown().filter { it.isFile }.sumOf { it.length() }

    private fun mb(bytes: Long): Long = (bytes + 1024 * 1024 - 1) / (1024 * 1024)

    override fun handleOnDestroy() {
        engines.values.forEach { it.close() }
        engines.clear()
        worker.shutdownNow()
        installer.shutdownNow()
        super.handleOnDestroy()
    }

    /** 去掉重复项（JS 侧只想拿到一份干净的语种表）。 */
    private fun normalizeTargets(targets: JSArray): JSArray {
        val seen = LinkedHashSet<String>()
        for (i in 0 until targets.length()) {
            val value = targets.getString(i) ?: continue
            if (value.isNotEmpty()) seen.add(value)
        }
        val out = JSArray()
        seen.forEach { out.put(it) }
        return out
    }

    private fun dirOf(uriOrPath: String): String {
        return if (uriOrPath.startsWith("file://")) {
            android.net.Uri.parse(uriOrPath).path ?: uriOrPath
        } else {
            uriOrPath
        }.let { File(it).absolutePath }
    }

    /**
     * 模型根目录。JS 侧可以不传：不传就用应用沙盒的 ``files/models``——
     * 这正是 Capacitor 的 Directory.Data 落地处，与识别模型 ``models/<kind>``
     * 同一层，翻译模型落在 ``models/mt-zh-en``。少一次「前端拼 URI、原生
     * 再解析回路径」的往返，也少一处 ``file://`` 与绝对路径混淆的机会。
     */
    private fun modelRoot(call: PluginCall): String {
        val given = call.getString("root")
        if (!given.isNullOrBlank()) return dirOf(given)
        return File(context.filesDir, "models").absolutePath
    }

    /**
     * 找一个方向到底用哪个目录，**先内部、后外部**。
     *
     * 内部 `files/models`（Capacitor 的 Directory.Data）只有 adb 调试包能写进去
     * ——release 包不可 `run-as`，签名后的 APK 里用户根本没法把 226 MB 的模型
     * 放进来，等于翻译功能装不上。所以再认一个位置：
     *
     *     /sdcard/Android/data/com.echonote.app/files/models/mt-zh-en/
     *
     * 这是 `getExternalFilesDir`，不需要任何存储权限，`adb push` 能写、
     * 文件管理器也能进。两处都放时以内部为准（旧安装方式的行为不变）。
     */
    private fun resolveDirectionDir(root: String, direction: String): File? {
        val name = "mt-$direction"
        val candidates = listOfNotNull(
            File(root, name),
            context.getExternalFilesDir(null)?.let { File(it, "models/$name") },
        )
        return candidates.firstOrNull { MtEngine.isInstalled(it) }
    }

    private companion object {
        /** 目标语种 → 方向目录名。模型目录是 mt-<源>-<目标>。 */
        val DIRECTION_BY_TARGET = mapOf("en" to "zh-en", "zh" to "en-zh")

        /** 方向目录名 → 目标语种（状态上报用，与上面互为反查）。 */
        val TARGET_BY_DIRECTION = mapOf("zh-en" to "en", "en-zh" to "zh")

        /**
         * 模型包地址。用 `latest` 而不是写死版本号：模型重导后只要挂到最新
         * Release 的同名资产上，旧安装就还能下到对的文件；反过来，**每次发版
         * 必须继续挂这两个 zip**，否则老用户的应用内下载会 404。
         */
        const val RELEASE_BASE = "https://github.com/Qsianuo/echonote/releases/latest/download/"
        const val UA = "EchoNote-Android/1.0"
    }
}

/**
 * 一个方向（如 mt-zh-en）的推理引擎。
 *
 * 贪心解码而非 beam search：手机上每步都要过一次 decoder，beam=4 就是四倍
 * 时间；译文短句（课堂口播）的质量差主要来自分词和术语，不在 beam 上。
 */
internal class MtEngine(
    private val dir: File,
    private val env: OrtEnvironment,
) : AutoCloseable {
    private val encoder: OrtSession
    private val decoderFirst: OrtSession
    private val decoderPast: OrtSession
    private val sourceVocab: SpEncoder
    private val targetVocab: SpDecoder
    private val layers: Int
    private val startId: Int
    private val eosId: Int
    private val vocabSize: Int

    init {
        val options = OrtSession.SessionOptions().apply {
            setIntraOpNumThreads(2)
            setOptimizationLevel(OrtSession.SessionOptions.OptLevel.ALL_OPT)
        }
        encoder = env.createSession(File(dir, ENCODER).absolutePath, options)
        decoderFirst = env.createSession(File(dir, DECODER).absolutePath, options)
        decoderPast = env.createSession(File(dir, DECODER_PAST).absolutePath, options)
        sourceVocab = SpEncoder.load(File(dir, SRC_VOCAB))
        targetVocab = SpDecoder.load(File(dir, TGT_VOCAB))

        // 层数不必自己配：past 输入名里最大的那个编号就是层数-1。
        var maxLayer = -1
        for (name in decoderPast.inputNames) {
            val match = Regex("past_key_values\\.(\\d+)\\.").find(name) ?: continue
            val index = match.groupValues[1].toInt()
            if (index > maxLayer) maxLayer = index
        }
        layers = maxLayer + 1
        if (layers <= 0) throw IllegalStateException("ONNX 里找不到 past_key_values，模型导出可能少了 with-past")

        // 首步 token 是 </s> 还是 <pad>——这是整段最容易踩的坑。HF 的 Marian
        // 配置里 decoder_start_token_id = pad_id(65000)，不是 eos。传成 </s>
        // 时解码器第一步直接吐 </s>，表现是"每句都翻出空串"，且不报任何错。
        val header = File(dir, SRC_VOCAB).useLines { it.firstOrNull() } ?: ""
        fun special(name: String, fallback: Int): Int =
            Regex("$name=(\\d+)").find(header)?.groupValues?.get(1)?.toIntOrNull() ?: fallback
        eosId = special("eos", 0)
        startId = special("pad", 65000)
        vocabSize = decoderFirst.outputInfo[LOGITS]?.let { (it as? ai.onnxruntime.TensorInfo)?.shape?.last()?.toInt() }
            ?: 65001
    }

    fun translate(text: String): String = synchronized(this) {
        val pieces = sourceVocab.encode(text)
        if (pieces.isEmpty()) return ""
        // Marian 要求 source 以 </s> 收尾（桌面端 app/translate.py 同理，补漏
        // 会让解码器进入重复循环：real-real-real-real）。
        val sourceIds = LongArray(pieces.size + 1)
        for (i in pieces.indices) sourceIds[i] = pieces[i].toLong()
        sourceIds[pieces.size] = eosId.toLong()

        val seq = sourceIds.size
        val inputIds = OnnxTensor.createTensor(env, LongBuffer.wrap(sourceIds), longArrayOf(1, seq.toLong()))
        val mask = OnnxTensor.createTensor(env, LongBuffer.wrap(LongArray(seq) { 1L }), longArrayOf(1, seq.toLong()))
        var encResult: OrtSession.Result? = null
        // 两份 past 的生命周期不同，必须分开存：
        //   · decoder.*（自注意力）每步都在长，从上一步的输出里取；
        //   · encoder.*（cross-attention）只在**首步**算一次，之后每步都复用
        //     首步那份。曾经每步都从上一步取，第二步就找不到 present.*.encoder.key
        //     ——增量图只吐 decoder 那组 present。
        var firstOut: OrtSession.Result? = null
        var lastOut: OrtSession.Result? = null
        val produced = ArrayList<Int>(64)
        var token = startId
        try {
            encResult = encoder.run(mapOf("input_ids" to inputIds, "attention_mask" to mask))
            val hidden = encResult.get("last_hidden_state").orElseThrow() as OnnxTensor

            for (step in 0 until MAX_NEW_TOKENS) {
                val session = if (step == 0) decoderFirst else decoderPast
                val stepIds = OnnxTensor.createTensor(
                    env, LongBuffer.wrap(longArrayOf(token.toLong())), longArrayOf(1, 1)
                )
                // 按会话自己声明的输入名逐个填，不能"塞满三个通用输入"：
                //   · 首步要 encoder_hidden_states（cross-attention 的 K/V 在这里算），
                //     增量步不要——多塞一个就是 Unknown input name。
                // 另外增量步要**两组** past，漏掉 encoder.* 那组，cross-attention
                // 就没有 key/value，模型照跑但译文崩。
                val feed = HashMap<String, OnnxTensor>(4 + layers * 4)
                for (name in session.inputNames) {
                    when {
                        name == "input_ids" -> feed[name] = stepIds
                        name == "encoder_attention_mask" || name == "attention_mask" -> feed[name] = mask
                        name == "encoder_hidden_states" -> feed[name] = hidden
                        name.startsWith("past_key_values") -> {
                            val present = "present" + name.removePrefix("past_key_values")
                            val source = if (name.contains(".encoder.")) firstOut else lastOut
                            feed[name] = source?.get(present)?.orElse(null) as? OnnxTensor
                                ?: throw IllegalStateException("上一轮缺少 $present")
                        }
                    }
                }
                val out = session.run(feed)
                stepIds.close()

                val next = argmax(out.get(LOGITS).orElseThrow() as OnnxTensor)
                if (step == 0) firstOut = out
                // 上一步的输出作为下一步的 past 输入，用完即关；首步那份要留到最后。
                if (lastOut != null && lastOut !== firstOut) lastOut.close()
                lastOut = out
                if (next == eosId) break
                produced.add(next)
                token = next
            }
        } finally {
            if (lastOut != null && lastOut !== firstOut) lastOut.close()
            firstOut?.close()
            encResult?.close()
            inputIds.close()
            mask.close()
        }
        targetVocab.decode(produced.toIntArray())
    }

    /** 取一句话里最后一个位置的 argmax（logits 形状 [1,1,V]）。 */
    private fun argmax(logits: OnnxTensor): Int {
        val buffer: FloatBuffer = logits.floatBuffer
        val total = buffer.remaining()
        if (total <= 0) return eosId
        var best = 0
        var bestScore = -Float.MAX_VALUE
        // 只扫最后一行：首步 [1,1,V] 与增量步一致，都只有一行。
        val rowStart = total - vocabSize
        for (i in 0 until vocabSize) {
            val value = buffer.get(rowStart + i)
            if (value > bestScore) {
                bestScore = value
                best = i
            }
        }
        return best
    }

    override fun close() {
        encoder.close()
        decoderFirst.close()
        decoderPast.close()
    }

    companion object {
        // 只认 int8 三件套：fp32 单方向 580 MB，两个方向过 1 GB，手机上不成立。
        // 量化后约 223 MB/方向（详见 tools/quantize_mt_onnx.py 的说明）。
        const val ENCODER = "encoder-int8.onnx"
        const val DECODER = "decoder-int8.onnx"
        const val DECODER_PAST = "decoder-past-int8.onnx"
        const val SRC_VOCAB = "mt-src-vocab.txt"
        const val TGT_VOCAB = "mt-tgt-vocab.txt"
        private const val LOGITS = "logits"
        private const val MAX_NEW_TOKENS = 128

        fun isInstalled(dir: File): Boolean {
            if (!dir.isDirectory) return false
            for (name in listOf(ENCODER, DECODER, DECODER_PAST, SRC_VOCAB, TGT_VOCAB)) {
                val file = File(dir, name)
                if (!file.isFile || file.length() == 0L) return false
            }
            return true
        }
    }
}
