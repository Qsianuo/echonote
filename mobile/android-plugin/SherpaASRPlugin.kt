package com.echonote.app

import android.Manifest
import android.content.pm.PackageManager
import android.net.Uri
import android.util.Base64
import androidx.core.content.ContextCompat
import com.getcapacitor.JSArray
import com.getcapacitor.JSObject
import com.getcapacitor.PermissionState
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin
import com.getcapacitor.annotation.Permission
import com.getcapacitor.annotation.PermissionCallback
import com.k2fsa.sherpa.onnx.FeatureConfig
import com.k2fsa.sherpa.onnx.OfflineModelConfig
import com.k2fsa.sherpa.onnx.OfflineRecognizer
import com.k2fsa.sherpa.onnx.OfflineRecognizerConfig
import com.k2fsa.sherpa.onnx.OfflineSenseVoiceModelConfig
import com.k2fsa.sherpa.onnx.OnlineModelConfig
import com.k2fsa.sherpa.onnx.OnlineRecognizer
import com.k2fsa.sherpa.onnx.OnlineRecognizerConfig
import com.k2fsa.sherpa.onnx.OnlineTransducerModelConfig
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.atomic.AtomicReference

/**
 * EchoNote 原生识别插件（Android）。
 *
 * 三个职责，与 web/native-sherpa.js 一一对应：
 *  · loadModel(model="streaming"|"final", dir=沙盒目录)  构建识别器
 *  · downloadModel(repo, files, dir)  模型下载：HttpURLConnection 流式写盘
 *    （256 KB 块），字节级进度经 downloadProgress 事件上报。为什么不走
 *    CapacitorHttp：它把整个响应读进内存再交 base64——228 MB 的模型必然
 *    OOM/假死（首启“卡在模型加载中”的实测根因）。
 *  · decodeStreaming(pcmB64, reset) / decodeFinal(pcmB64)
 *
 * Kotlin 绑定（com.k2fsa.sherpa.onnx 包）取自 sherpa-onnx v1.13.8 的
 * kotlin-api-examples；JNI .so 在 app/src/main/jniLibs/。
 */
@CapacitorPlugin(
    name = "SherpaASR",
    permissions = [
        Permission(alias = "microphone", strings = [Manifest.permission.RECORD_AUDIO])
    ],
)
class SherpaASRPlugin : Plugin() {

    private val online = AtomicReference<OnlineRecognizer?>(null)
    private val offline = AtomicReference<OfflineRecognizer?>(null)
    private var onlineStream: com.k2fsa.sherpa.onnx.OnlineStream? = null
    private val lock = Object()

    // ------------------------------------------------------------------
    // 麦克风运行时权限
    //
    // WebView 里的 getUserMedia 有一道前置链：App 先持有 RECORD_AUDIO 原生
    // 权限，Capacitor 的 WebChromeClient 才会把 WebView 的录音请求自动放行
    // （缺权限时用户看到的是“去系统设置开麦克风”一类的死胡同提示）。
    // 因此在网页层发起录音之前，由这里主动请求一次系统权限弹窗。
    // ------------------------------------------------------------------

    @PluginMethod
    fun ensureMic(call: PluginCall) {
        if (micGranted()) {
            val ret = JSObject()
            ret.put("granted", true)
            call.resolve(ret)
            return
        }
        requestPermissionForAlias("microphone", call, "micResult")
    }

    @PermissionCallback
    fun micResult(call: PluginCall) {
        val ret = JSObject()
        ret.put("granted", micGranted())
        call.resolve(ret)
    }

    private fun micGranted(): Boolean {
        return ContextCompat.checkSelfPermission(
            context, Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
    }

    // ------------------------------------------------------------------
    // 模型下载（流式 + 字节级进度）
    // ------------------------------------------------------------------

    @PluginMethod
    fun downloadModel(call: PluginCall) {
        val repo = call.getString("repo") ?: return call.reject("缺少 repo")
        val files: List<String> =
            (call.getArray("files") ?: return call.reject("缺少 files"))
                .let { arr -> (0 until arr.length()).map { arr.getString(it) } }
        val dir = dirOf(call.getString("dir") ?: return call.reject("缺少 dir"))
        val parent = File(dir)
        if (!parent.isDirectory && !parent.mkdirs()) {
            return call.reject("无法创建模型目录 $dir")
        }

        try {
            // 已存在的文件计入「已完成字节」，进度条不从 0 开始。
            var baseReceived = 0L
            val existing = BooleanArray(files.size)
            for (i in files.indices) {
                val f = File(parent, files[i])
                if (f.isFile && f.length() > 0) {
                    existing[i] = true
                    baseReceived += f.length()
                }
            }

            var received = baseReceived

            // 先 HEAD 拿每个待下载文件的大小：总量已知，进度条百分比才可信。
            val sizes = LongArray(files.size)
            var allKnown = true
            for (i in files.indices) {
                if (existing[i]) continue
                sizes[i] = headContentLength(repo, files[i])
                if (sizes[i] <= 0) allKnown = false
            }
            val total: Long =
                if (allKnown) {
                    baseReceived + files.indices.filter { !existing[it] }.sumOf { sizes[it] }
                } else {
                    -1L
                }

            for (i in files.indices) {
                val name = files[i]
                val target = File(parent, name)
                if (existing[i]) {
                    notifyProgress(name, i, files.size, received, total)
                    continue
                }

                val conn = URL("https://hf-mirror.com/$repo/resolve/main/$name")
                    .openConnection() as HttpURLConnection
                conn.connectTimeout = 15000
                conn.readTimeout = 60000
                conn.instanceFollowRedirects = true
                conn.setRequestProperty("User-Agent", "EchoNote-Mobile/0.1")
                try {
                    val code = conn.responseCode
                    if (code != 200) throw IllegalStateException("HTTP $code for $name")
                    val expected = sizes[i]
                    val part = File(parent, "$name.part")
                    var lastNotify = 0L
                    FileOutputStream(part).use { out ->
                        conn.inputStream.use { input ->
                            val buf = ByteArray(256 * 1024)
                            var fileBytes = 0L
                            while (true) {
                                val n = input.read(buf)
                                if (n < 0) break
                                out.write(buf, 0, n)
                                fileBytes += n
                                received += n
                                val now = System.currentTimeMillis()
                                if (now - lastNotify > 300) {
                                    lastNotify = now
                                    notifyProgress(name, i, files.size, received, total)
                                }
                            }
                            if (expected > 0 && fileBytes != expected) {
                                part.delete()
                                throw IllegalStateException(
                                    "$name 不完整：$fileBytes / $expected"
                                )
                            }
                        }
                    }
                    if (!part.renameTo(target)) {
                        throw IllegalStateException("无法落盘 $name")
                    }
                    notifyProgress(name, i, files.size, received, total)
                } finally {
                    conn.disconnect()
                }
            }

            val ret = JSObject()
            ret.put("ok", true)
            call.resolve(ret)
        } catch (e: Exception) {
            call.reject("模型下载失败: ${e.message}")
        }
    }

    /** HEAD 拿 Content-Length（跟随重定向）；失败返回 -1。 */
    private fun headContentLength(repo: String, name: String): Long {
        return try {
            val conn = URL("https://hf-mirror.com/$repo/resolve/main/$name")
                .openConnection() as HttpURLConnection
            conn.connectTimeout = 15000
            conn.readTimeout = 30000
            conn.requestMethod = "HEAD"
            conn.instanceFollowRedirects = true
            conn.setRequestProperty("User-Agent", "EchoNote-Mobile/0.1")
            val len = if (conn.responseCode == 200) conn.contentLengthLong else -1L
            conn.disconnect()
            len
        } catch (e: Exception) {
            -1L
        }
    }

    private fun notifyProgress(file: String, fileIndex: Int, fileCount: Int, received: Long, total: Long) {
        val data = JSObject()
        data.put("file", file)
        data.put("fileIndex", fileIndex)
        data.put("fileCount", fileCount)
        data.put("received", received)
        data.put("total", if (total > 0) total else 0)
        notifyListeners("downloadProgress", data)
    }

    // ------------------------------------------------------------------
    // 识别器加载与解码
    // ------------------------------------------------------------------

    @PluginMethod
    fun loadModel(call: PluginCall) {
        val model = call.getString("model") ?: return call.reject("缺少 model")
        val dir = dirOf(call.getString("dir") ?: return call.reject("缺少 dir"))
        try {
            synchronized(lock) {
                when (model) {
                    "streaming" -> {
                        val config = OnlineRecognizerConfig(
                            featConfig = FeatureConfig(sampleRate = 16000, featureDim = 80),
                            modelConfig = OnlineModelConfig(
                                transducer = OnlineTransducerModelConfig(
                                    encoder = "$dir/encoder-epoch-99-avg-1.int8.onnx",
                                    decoder = "$dir/decoder-epoch-99-avg-1.int8.onnx",
                                    joiner = "$dir/joiner-epoch-99-avg-1.int8.onnx",
                                ),
                                tokens = "$dir/tokens.txt",
                                numThreads = 2,
                            ),
                            decodingMethod = "greedy_search",
                        )
                        online.set(OnlineRecognizer(assetManager = null, config = config))
                        onlineStream?.release()
                        onlineStream = null
                    }
                    "final" -> {
                        val config = OfflineRecognizerConfig(
                            featConfig = FeatureConfig(sampleRate = 16000, featureDim = 80),
                            modelConfig = OfflineModelConfig(
                                senseVoice = OfflineSenseVoiceModelConfig(
                                    model = "$dir/model.int8.onnx",
                                    language = "",
                                    useInverseTextNormalization = true,
                                ),
                                tokens = "$dir/tokens.txt",
                                numThreads = 2,
                            ),
                        )
                        offline.set(OfflineRecognizer(assetManager = null, config = config))
                    }
                    else -> return call.reject("未知模型 $model")
                }
            }
            val ret = JSObject()
            ret.put("ok", true)
            call.resolve(ret)
        } catch (e: Exception) {
            call.reject("模型加载失败: ${e.message}")
        }
    }

    @PluginMethod
    fun decodeStreaming(call: PluginCall) {
        val recognizer = online.get() ?: return call.reject("streaming 模型未加载")
        val samples = pcmFrom(call) ?: return call.reject("音频解码失败")
        val reset = call.getBoolean("reset", false) ?: false
        try {
            synchronized(lock) {
                if (reset || onlineStream == null) {
                    onlineStream?.release()
                    onlineStream = recognizer.createStream()
                }
                val stream = onlineStream!!
                stream.acceptWaveform(samples, 16000)
                while (recognizer.isReady(stream)) recognizer.decode(stream)
                val text = recognizer.getResult(stream).text ?: ""
                val ret = JSObject()
                ret.put("text", text)
                call.resolve(ret)
            }
        } catch (e: Exception) {
            call.reject("流式解码失败: ${e.message}")
        }
    }

    @PluginMethod
    fun decodeFinal(call: PluginCall) {
        val recognizer = offline.get() ?: return call.reject("final 模型未加载")
        val samples = pcmFrom(call) ?: return call.reject("音频解码失败")
        try {
            synchronized(lock) {
                val stream = recognizer.createStream()
                stream.acceptWaveform(samples, 16000)
                recognizer.decode(stream)
                val text = recognizer.getResult(stream).text ?: ""
                stream.release()
                val ret = JSObject()
                ret.put("text", text)
                call.resolve(ret)
            }
        } catch (e: Exception) {
            call.reject("离线解码失败: ${e.message}")
        }
    }

    /** base64 Int16 PCM（小端）→ Float32 [-1,1]。 */
    private fun pcmFrom(call: PluginCall): FloatArray? {
        val b64 = call.getString("pcmB64") ?: return null
        val bytes = Base64.decode(b64, Base64.NO_WRAP)
        if (bytes.size < 2 || bytes.size % 2 != 0) return null
        val count = bytes.size / 2
        val out = FloatArray(count)
        for (i in 0 until count) {
            val lo = bytes[i * 2].toInt() and 0xff
            val hi = bytes[i * 2 + 1].toInt()
            val v = (hi shl 8) or lo
            out[i] = v / 32768f
        }
        return out
    }

    /** Capacitor Filesystem 返回的 file:// URI 或绝对路径，统一成 File。 */
    private fun dirOf(uriOrPath: String): String {
        return if (uriOrPath.startsWith("file://")) {
            Uri.parse(uriOrPath).path ?: uriOrPath
        } else {
            uriOrPath
        }.let { File(it).absolutePath }
    }
}
