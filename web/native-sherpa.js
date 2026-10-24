/**
 * SherpaASR 原生插件封装（移动端）。
 *
 * 原生侧（android-plugin/ 与 ios-plugin/）用 sherpa-onnx 跑两个模型：
 *   · streaming —— zipformer 中文流式（实时字幕，喂增量音频）
 *   · final     —— SenseVoice 离线（整句定稿，自带标点）
 *
 * 本文件只做三件事：模型下载（首启一次，之后飞行模式可用）、base64 PCM
 * 的桥传输、解码调用。不依赖任何 npm 包——在壳里通过
 * window.Capacitor.Plugins 直接拿原生插件，网页端（桌面调试）没有该对象，
 * 所有方法抛 NotAvailable，由 engine 决定降级。
 */

const PLUGIN = () => {
  const cap = typeof window !== 'undefined' && window.Capacitor;
  return cap && cap.Plugins && cap.Plugins.SherpaASR ? cap.Plugins.SherpaASR : null;
};

// 与 PC 端同一来源（csukuangfj 在 HF 的官方镜像，文件名已按仓库实存核对）。
// 移动端流式模型选 14M int8 三件套：下载量约 45 MB，手机 CPU 实时富余；
// 后续可换 2025-06-30 的更大 zh int8 版（native-boot 的进度总数随之调整）。
const MODEL_SOURCES = {
  streaming: {
    repo: 'csukuangfj/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23',
    files: [
      'encoder-epoch-99-avg-1.int8.onnx',
      'decoder-epoch-99-avg-1.int8.onnx',
      'joiner-epoch-99-avg-1.int8.onnx',
      'tokens.txt',
    ],
  },
  final: {
    repo: 'csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17',
    files: ['model.int8.onnx', 'tokens.txt'],
  },
};

export const sherpa = {
  available() {
    return !!PLUGIN();
  },

  /** 麦克风运行时权限：网页层 getUserMedia 之前必须先过这里——原生层
   *  没拿到 RECORD_AUDIO 时，WebView 的录音请求会被系统挡在半路，
   *  用户看到的是“去浏览器/设置里开麦克风”的死胡同提示。 */
  async ensureMic() {
    const plugin = PLUGIN();
    if (!plugin) return true; // 桌面调试环境：浏览器自己管权限
    const { granted } = await plugin.ensureMic();
    return !!granted;
  },

  /** 首启模型下载：**原生层流式写盘**（SherpaASRPlugin.downloadModel），
   *  逐块落盘不占内存，进度经 downloadProgress 事件上报字节级数值。
   *  （曾用 CapacitorHttp 整文件进内存再写盘——228 MB 的模型会假死，这正是
   *  首启“卡在模型加载中”的根因。）已存在的文件跳过；返回模型目录。 */
  async ensureModel(kind, { onProgress } = {}) {
    const plugin = PLUGIN();
    if (!plugin) throw new Error('SherpaASR 插件不可用（非原生壳）');
    const spec = MODEL_SOURCES[kind];
    if (!spec) throw new Error(`未知模型 ${kind}`);

    const { Filesystem, Directory } = await capacitorModules();
    const dir = `models/${kind}`;
    let dirUri;
    try {
      const res = await Filesystem.getUri({ path: dir, directory: Directory.Data });
      dirUri = res.uri;
    } catch {
      await Filesystem.mkdir({ path: dir, directory: Directory.Data, recursive: true });
      const res = await Filesystem.getUri({ path: dir, directory: Directory.Data });
      dirUri = res.uri;
    }

    const forward = (cb) => {
      if (!onProgress || !cb) return null;
      return plugin.addListener('downloadProgress', (event) => {
        onProgress({
          file: event.file,
          fileIndex: event.fileIndex,
          fileCount: event.fileCount,
          received: event.received,
          total: event.total,
        });
      });
    };

    const listener = forward(onProgress);
    try {
      // 原生侧逐文件跳过已存在者并流式下载其余，全部完成后 resolve。
      await plugin.downloadModel({
        repo: spec.repo,
        files: spec.files,
        dir: dirUri,
      });
    } finally {
      if (listener && listener.remove) await listener.remove();
    }
    await plugin.loadModel({ model: kind, dir: dirUri });
    return dirUri;
  },

  /** 喂入一段增量音频（Float32Array，16 kHz mono）。utterance 开始时
   *  reset=true，原生侧新建流；返回当前假设文本。 */
  async decodeStreaming(audio, { reset = false } = {}) {
    const plugin = PLUGIN();
    if (!plugin) throw new Error('SherpaASR 插件不可用');
    const { text } = await plugin.decodeStreaming({
      pcmB64: floatToBase64Pcm16(audio),
      reset,
    });
    return text || '';
  },

  /** 整句离线定稿（SenseVoice，带标点）。 */
  async decodeFinal(audio) {
    const plugin = PLUGIN();
    if (!plugin) throw new Error('SherpaASR 插件不可用');
    const { text } = await plugin.decodeFinal({
      pcmB64: floatToBase64Pcm16(audio),
    });
    return text || '';
  },
};

async function capacitorModules() {
  const cap = window.Capacitor;
  const fs = (cap && cap.Plugins && cap.Plugins.Filesystem) || null;
  if (!fs) throw new Error('缺 Filesystem 插件');
  const Directory = fs.Directory || { Data: 'DATA' };
  return { Filesystem: fs, Directory };
}

function floatToBase64Pcm16(audio) {
  const pcm = new Int16Array(audio.length);
  for (let i = 0; i < audio.length; i++) {
    const s = Math.max(-1, Math.min(1, audio[i]));
    pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return bytesToBase64(new Uint8Array(pcm.buffer));
}

function bytesToBase64(bytes) {
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}
