/**
 * EchoNote 原生壳适配层（Capacitor）。
 *
 * 只在 window.Capacitor 存在时激活；桌面/局域网环境加载本文件等于空操作。
 * 激活后做三件事，让现有 app.js 不改一行逻辑地跑在原生壳里：
 *
 *  1. 拦截 /api/* 的 fetch：config/ready/mode 等由本地引擎应答
 *     （原生壳里没有 FastAPI）；
 *  2. 用 NativeSocket 替换 window.WebSocket：音频帧进 MobileEngine，
 *     识别结果按服务端协议形状（partial/final/refine/done）回吐；
 *  3. 首启下载两个模型（streaming + final）到应用沙盒——下载在原生层
 *     流式写盘（SherpaASRPlugin.downloadModel），字节级进度经
 *     /api/ready 的 `download` 字段透出（app.js 据此显示进度条文案）。
 *
 * 本文件是 ES 模块：在整页解析完之后、DOMContentLoaded 之前执行；
 * app.js 的 boot() 因此挂到 DOMContentLoaded，保证补丁先于启动安装。
 */

import { MobileEngine } from './native-engine.js?v=20261024g';
import { MODES } from './native-vad.js?v=20261024g';
import { sherpa } from './native-sherpa.js?v=20261024g';

const isNative = typeof window !== 'undefined' && !!window.Capacitor;
if (isNative) install();

function install() {
  // ------------------------------------------------------------ state
  let ready = false;
  let readyError = null;
  // 字节级下载进度（由 sherpa.ensureModel 的监听更新）。
  const download = { received: 0, total: 0, file: '', fileIndex: 0, fileCount: 0 };
  let activeSocket = null;
  let doneTimer = null;
  let lastStatusPush = 0;

  const engine = new MobileEngine({
    onPartial: (p) => emit({ type: 'partial', ...p }),
    onFinal: (f) => emit(f),
    onRefine: (r) => emit(r),
    onWarning: (w) => emit({ type: 'warning', retryable: true, ...w }),
  });
  engine.setMode(localStorage.getItem('echonote-mode') || 'personal');

  // ------------------------------------------------------ 模型下载
  async function loadModels() {
    if (ready) return;
    readyError = null;
    resetDownload();
    try {
      await sherpa.ensureModel('streaming', { onProgress });
      await sherpa.ensureModel('final', { onProgress });
      ready = true;
      pushStatus(true);
    } catch (error) {
      readyError = `模型下载失败：${error && error.message ? error.message : error}`;
      pushStatus(true);
    }
  }

  function resetDownload() {
    download.received = 0;
    download.total = 0;
    download.file = '';
    download.fileIndex = 0;
    download.fileCount = 0;
  }

  function onProgress(event) {
    download.received = event.received || 0;
    download.total = event.total || 0;
    download.file = event.file || '';
    download.fileIndex = event.fileIndex || 0;
    download.fileCount = event.fileCount || 0;
    // 节流：进度事件很密，状态推送 500 ms 一次足够（app.js 还有 5 s 轮询兜底）。
    const now = Date.now();
    if (now - lastStatusPush > 500) pushStatus(true);
  }

  function downloadPercent() {
    if (!download.total) return 0;
    return Math.min(100, Math.round((download.received / download.total) * 100));
  }

  function statusPayload() {
    const downloading = !ready && !readyError && download.fileCount > 0;
    const pct = downloadPercent();
    const label = ready
      ? '本地识别'
      : downloading
        ? `模型下载 ${pct}%`
        : '准备中';
    return {
      type: 'status',
      ready,
      // 页脚胶囊/模型标签位就是现成的进度显示。
      model: label,
      model_key: 'native',
      model_label: label,
      error: readyError,
      retrying: false,
      retry_count: 1,
      backend: 'native',
      live_model: 'native',
      live_model_key: 'native',
      live_model_label: '本地流式',
      live_ready: true,
      live_separate: true,
      offline_ready: true,
      refine: true,
      rtf: null,
      live_ms: null,
      // app.js 据此渲染进度文案；桌面服务不返回该字段，行为不变。
      download: downloading
        ? {
            percent: pct,
            receivedMb: Math.round((download.received / 1048576) * 10) / 10,
            totalMb: Math.round((download.total / 1048576) * 10) / 10,
            file: download.file,
            fileIndex: download.fileIndex,
            fileCount: download.fileCount,
          }
        : null,
    };
  }

  function pushStatus(force = false) {
    const now = Date.now();
    if (!force && now - lastStatusPush < 500) return;
    lastStatusPush = now;
    emit(statusPayload());
  }

  loadModels();

  // 麦克风运行时权限与模型下载并行请求：弹窗出现在首屏，用户点允许的
  // 时候模型已经在下了。拒绝也不阻塞下载，只是录音时会被 getUserMedia
  // 拒绝并以现有错误提示呈现。
  sherpa
    .ensureMic()
    .then((granted) => {
      if (!granted) console.warn('[native-boot] 麦克风权限被拒绝，录音将不可用');
    })
    .catch((error) => console.warn('[native-boot] 麦克风权限请求失败:', error));

  // -------------------------------------------------- 演示音源（?demo）
  //
  // 模拟器/无麦环境下的确定性测试通道：demo 模式把 AudioWorkletNode 换成
  // 假节点，录音开始后按 100 ms 帧把 web/sample.wav（真实人声 16 kHz PCM）
  // 灌进 handlePcm——账本、落盘、波形时间线、识别管线全部照常工作，导出
  // 与回放也都是这段真实音频。真实手机不带 ?demo 时不受影响。
  //   激活：URL 带 ?demo=1（或 localStorage.echonote-demo=1，激活后记住），
  //   每段录音里把样本循环播两遍后停。
  const demoRequested =
    new URLSearchParams(location.search).has('demo') ||
    localStorage.getItem('echonote-demo') === '1';
  if (demoRequested) localStorage.setItem('echonote-demo', '1');

  if (demoRequested) {
    const RealWorkletNode = window.AudioWorkletNode;
    let demoTimer = null;
    let demoHandler = null;
    window.AudioWorkletNode = function (_ctx, name, opts) {
      // 图连接需要真节点（app.js 会把麦克风源连上来）；demo 只劫持输出：
      // 真 port 的 onmessage 永远不会被设置，真实帧（模拟器里是静音）被
      // 屏蔽，demo 帧经垫片进 handlePcm。
      const real = new RealWorkletNode(_ctx, name, opts);
      if (name !== 'pcm16') return real;
      const shim = {
        set onmessage(fn) {
          demoHandler = fn;
          startDemoFeed();
        },
        get onmessage() {
          return demoHandler;
        },
        postMessage(msg) {
          real.port.postMessage(msg);
        },
      };
      Object.defineProperty(real, 'port', { value: shim, configurable: true });
      return real;
    };

    async function startDemoFeed() {
      if (demoTimer) {
        clearInterval(demoTimer);
        demoTimer = null;
      }
      if (!ready) return; // 模型没就绪不喂音，录音会自己报 model_not_ready
      try {
        const res = await realFetch('sample.wav?v=' + ASSET_VERSION);
        const buf = await res.arrayBuffer();
        const view = new DataView(buf);
        // 极简 WAV 解析：找 data 块（PCM16 mono 16k，本项目的固定格式）。
        let offset = 12;
        let pcm = null;
        while (offset + 8 <= view.byteLength) {
          const id = String.fromCharCode(
            view.getUint8(offset),
            view.getUint8(offset + 1),
            view.getUint8(offset + 2),
            view.getUint8(offset + 3)
          );
          const size = view.getUint32(offset + 4, true);
          if (id === 'data') {
            pcm = new Int16Array(buf, offset + 8, Math.floor(size / 2));
            break;
          }
          offset += 8 + size + (size % 2);
        }
        if (!pcm) return;
        const frameSize = 1600; // 100 ms
        const loops = 2;
        let pos = 0;
        demoTimer = setInterval(() => {
          const frame = new Int16Array(frameSize);
          for (let i = 0; i < frameSize; i++) {
            if (pos >= pcm.length * loops) {
              clearInterval(demoTimer);
              demoTimer = null;
              break;
            }
            frame[i] = pcm[pos % pcm.length] || 0;
            pos += 1;
          }
          if (demoHandler) demoHandler({ data: frame.buffer });
        }, 100);
      } catch (error) {
        console.warn('[native-boot] 演示音源失败:', error);
      }
    }
  }

  // --------------------------------------------------------- fetch 拦截
  const realFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : (input && input.url) || String(input);
    if (url && url.startsWith('/api/')) return api(url, init);
    return realFetch(input, init);
  };

  function json(body, status = 200) {
    return new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  async function api(url, init) {
    const method = ((init && init.method) || 'GET').toUpperCase();
    const path = url.split('?')[0];
    let body = null;
    try {
      body = init && init.body ? JSON.parse(init.body) : null;
    } catch {
      body = null;
    }

    if (path === '/api/config') {
      return json({
        version: '1.0.0-mobile',
        stage: 'tech-preview',
        protocol: '4',
        asr_backend: 'native',
        model: 'native',
        model_label: '本地识别',
        model_key: 'native',
        offline_ready: true,
        offline_mode: true,
        sample_rate: 16000,
        language: 'zh',
        languages: LANGUAGES,
        live_step_s: 0.6,
        live_captions: true,
        live_model: 'native',
        live_model_key: 'native',
        live_model_label: '本地流式',
        live_separate: true,
        refine: true,
        max_record_minutes: 90,
        min_frame_ms: 100,
        max_frame_bytes: 32768,
        mode: engine.mode,
        hotwords: '',
      });
    }
    if (path === '/api/ready' || path === '/api/status') return json(statusPayload());
    if (path === '/api/models')
      return json({ current: 'native', current_path: null, offline_ready: true, items: [] });
    if (path === '/api/mode' && method === 'POST') {
      const name = body && body.mode;
      if (!MODES[name]) return json({ ok: false, error: `未知模式 ${name}` }, 400);
      engine.setMode(name);
      localStorage.setItem('echonote-mode', name);
      return json({ ok: true, mode: name });
    }
    if (path === '/api/model')
      return json({ ok: false, error: '移动端使用内置模型，无需选择' }, 400);
    if (path === '/api/retry') {
      loadModels();
      return json(statusPayload());
    }
    return json({ ok: false, error: '原生壳内不提供该接口' }, 404);
  }

  // ------------------------------------------------------- WebSocket 替换
  class NativeSocket {
    constructor(url) {
      this.url = url;
      this.readyState = NativeSocket.OPEN;
      this.binaryType = 'arraybuffer';
      this._listeners = {};
      activeSocket = this;
      // open 异步派发：connect() 在构造之后才挂监听器。顺手补发一次当前
      // 状态——install 时的 pushStatus 那时还没有监听者，不补发的话首屏
      // 只能等下一次轮询。
      queueMicrotask(() => {
        this._emit('open', { target: this });
        pushStatus(true);
      });
    }

    addEventListener(type, fn) {
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    }

    removeEventListener(type, fn) {
      const list = this._listeners[type] || [];
      const i = list.indexOf(fn);
      if (i >= 0) list.splice(i, 1);
    }

    send(data) {
      if (typeof data === 'string') {
        handleJson(data);
      } else if (data instanceof ArrayBuffer) {
        handleAudio(data);
      }
    }

    close() {
      if (this.readyState === NativeSocket.CLOSED) return;
      this.readyState = NativeSocket.CLOSED;
      if (activeSocket === this) activeSocket = null;
      engine.endSession();
      this._emit('close', { target: this });
    }

    _emit(type, event) {
      for (const fn of this._listeners[type] || []) {
        try {
          fn({ target: this, ...event });
        } catch (error) {
          console.error('[native-boot] listener failed:', error);
        }
      }
    }
  }
  NativeSocket.CONNECTING = 0;
  NativeSocket.OPEN = 1;
  NativeSocket.CLOSING = 2;
  NativeSocket.CLOSED = 3;
  window.WebSocket = NativeSocket;

  function handleJson(text) {
    let msg;
    try {
      msg = JSON.parse(text);
    } catch {
      return;
    }
    if (msg.type === 'config') {
      // app.js 的 beginSession() 每次开录/续录都会带新 epoch——正是引擎
      // 重建切句器（读最新情境预设）的时机。
      if (Number(msg.epoch) > engine.epoch) engine.beginSession();
    } else if (msg.type === 'flush') {
      engine.flushUtterance();
      armDoneTimer();
    }
  }

  function handleAudio(buffer) {
    cancelDoneTimer();
    const pcm = new Int16Array(buffer);
    const floats = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) floats[i] = pcm[i] / 32768;
    engine.push(floats);
  }

  // 停止/暂停都会发 flush；「最后一段」何时落定无从得知，用「flush 后
  // 1.2 s 无新音频」近似会话收尾，补发 done 让界面提示归位。暂停超过
  // 1.2 s 也会触发一次 done——只是提示文案提前切换，无功能影响。
  function armDoneTimer() {
    cancelDoneTimer();
    doneTimer = setTimeout(() => {
      doneTimer = null;
      emit({ type: 'done' });
    }, 1200);
  }

  function cancelDoneTimer() {
    if (doneTimer) {
      clearTimeout(doneTimer);
      doneTimer = null;
    }
  }

  function emit(obj) {
    if (activeSocket) {
      activeSocket._emit('message', { data: JSON.stringify(obj) });
    }
  }

  // 与 main.py 的 LANGUAGES 保持一致（原生壳内 SenseVoice 自动识别语言，
  // 下拉只是 UI 惯性，选什么都行）。
  const LANGUAGES = [
    { code: 'auto', label: '自动识别' },
    { code: 'zh', label: '中文' },
    { code: 'en', label: 'English' },
    { code: 'ja', label: '日本語' },
    { code: 'ko', label: '한국어' },
  ];
}
