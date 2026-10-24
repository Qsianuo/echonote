import Foundation
import Capacitor

/**
 * EchoNote 原生识别插件（iOS）。
 *
 * 与 android-plugin/SherpaASRPlugin.kt 同一 API：
 *  · loadModel(model:dir:)        构建识别器（streaming / final）
 *  · decodeStreaming(pcmB64:reset:)  zipformer 流式
 *  · decodeFinal(pcmB64:)            SenseVoice 离线
 *
 * 依赖：sherpa-onnx 预编译 xcframework（GitHub Releases 的
 * sherpa-onnx-v*-*-ios.tar.bz2，含 sherpa_onnx Swift 模块）。把它拖进
 * Xcode 工程并链接（README-mobile.md 有逐步说明）。
 */
@objc(SherpaASRPlugin)
public class SherpaASRPlugin: CAPPlugin {
    private var online: OnlineRecognizer?
    private var offline: OfflineRecognizer?
    private var onlineStream: OnlineStream?
    private let lock = NSLock()

    @objc func loadModel(_ call: CAPPluginCall) {
        guard let model = call.getString("model"),
              let dirRaw = call.getString("dir") else {
            call.reject("缺少 model / dir")
            return
        }
        // Capacitor Filesystem 给的是 file:// URL 或绝对路径。
        let dir = dirRaw.hasPrefix("file://")
            ? URL(string: dirRaw)?.path ?? dirRaw
            : dirRaw

        lock.lock()
        defer { lock.unlock() }
        do {
            switch model {
            case "streaming":
                let config = OnlineRecognizerConfig(
                    modelConfig: OnlineModelConfig(
                        transducer: OnlineTransducerModelConfig(
                            encoder: "\(dir)/encoder.int8.onnx",
                            decoder: "\(dir)/decoder-epoch-99-avg-1.onnx",
                            joiner: "\(dir)/joiner-epoch-99-avg-1.onnx"
                        ),
                        tokens: "\(dir)/tokens.txt",
                        numThreads: 2,
                        sampleRate: 16000,
                        featureDim: 80
                    ),
                    decodingMethod: "greedy_search"
                )
                online = try OnlineRecognizer(assetManager: nil, config: config)
                onlineStream = nil
            case "final":
                let config = OfflineRecognizerConfig(
                    modelConfig: OfflineModelConfig(
                        senseVoice: OfflineSenseVoiceModelConfig(
                            model: "\(dir)/model.int8.onnx",
                            language: "",
                            useItn: true
                        ),
                        tokens: "\(dir)/tokens.txt",
                        numThreads: 2,
                        sampleRate: 16000,
                        featureDim: 80
                    )
                )
                offline = try OfflineRecognizer(assetManager: nil, config: config)
            default:
                call.reject("未知模型 \(model)")
                return
            }
            call.resolve(["ok": true])
        } catch {
            call.reject("模型加载失败: \(error.localizedDescription)")
        }
    }

    @objc func decodeStreaming(_ call: CAPPluginCall) {
        guard let recognizer = online else {
            call.reject("streaming 模型未加载")
            return
        }
        guard let samples = pcm(from: call) else {
            call.reject("音频解码失败")
            return
        }
        let reset = call.getBool("reset") ?? false

        lock.lock()
        defer { lock.unlock() }
        if reset || onlineStream == nil {
            onlineStream = recognizer.createStream()
        }
        guard let stream = onlineStream else {
            call.reject("无法创建流")
            return
        }
        stream.acceptWaveform(16000, samples)
        while recognizer.isReady(stream) {
            recognizer.decode(stream)
        }
        call.resolve(["text": recognizer.getResult(stream).text])
    }

    @objc func decodeFinal(_ call: CAPPluginCall) {
        guard let recognizer = offline else {
            call.reject("final 模型未加载")
            return
        }
        guard let samples = pcm(from: call) else {
            call.reject("音频解码失败")
            return
        }
        lock.lock()
        defer { lock.unlock() }
        let stream = recognizer.createStream()
        stream.acceptWaveform(16000, samples)
        recognizer.decode(stream)
        call.resolve(["text": recognizer.getResult(stream).text])
    }

    /// base64 Int16 PCM → [Float]
    private func pcm(from call: CAPPluginCall) -> [Float]? {
        guard let b64 = call.getString("pcmB64"),
              let data = Data(base64Encoded: b64),
              data.count % 2 == 0 else { return nil }
        var out = [Float](repeating: 0, count: data.count / 2)
        data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            for i in 0 ..< out.count {
                let v = Int16(littleEndian: raw.load(fromByteOffset: i * 2, as: Int16.self))
                out[i] = Float(v) / 32768.0
            }
        }
        return out
    }
}
