/**
 * EchoNote 原生壳引导（Capacitor）。
 *
 * 只在 window.Capacitor 存在时激活；桌面/局域网环境加载本文件等于空操作。
 *
 * v1.0.0 起，原生集成走 **transport.js 的显式契约**（registerNative），
 * 不再替换 window.WebSocket、不再拦截 fetch——传输层的会话与 /api 应答
 * 在此注册，app.js 通过 EchoNoteTransport 使用它们。
 *
 * 职责：
 *  1. 模型引导：首启下载两个模型（streaming + final）到应用沙盒——下载在
 *     原生层流式写盘（SherpaASRPlugin.downloadModel），字节级进度经
 *     /api/ready 的 `download` 字段透出（app.js 据此显示进度条文案）；
 *  2. 识别引擎：MobileEngine（采集帧进，协议消息出）；
 *  3. /api/* 本地应答：config / ready / mode / models / retry；
 *  4. 麦克风运行时权限（getUserMedia 的前置）。
 *
 * 本文件是 ES 模块：在整页解析完之后、DOMContentLoaded 之前执行；
 * app.js 的 boot() 因此挂到 DOMContentLoaded，保证注册先于启动安装。
 */

import { MobileEngine } from './native-engine.js?v=f9b3a1a1e2';
import { MODES } from './native-vad.js?v=f9b3a1a1e2';
import { sherpa } from './native-sherpa.js?v=f9b3a1a1e2';

const isNative = typeof window !== 'undefined' && !!window.Capacitor;
if (isNative) install();

function install() {
  // ------------------------------------------------------------ state
  let ready = false;
  let readyError = null;
  // 字节级下载进度（由 sherpa.ensureModel 的监听更新）。
  const download = { received: 0, total: 0, file: '', fileIndex: 0, fileCount: 0 };
  let activeSession = null;
  let doneTimer = null;
  let lastStatusPush = 0;

  const engine = new MobileEngine({
    onPartial: (p) => emit({ type: 'partial', ...p }),
    onFinal: (f) => emit(f),
    onRefine: (r) => emit(r),
    onWarning: (w) => emit({ type: 'warning', retryable: true, ...w }),
  });
  engine.setMode(localStorage.getItem('echonote-mode') || 'personal');

  // ------------------------------------------------------ 模型引导
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
    // 节流：进度事件很密，状态推送 500 ms 一次足够（UI 还有 5 s 轮询兜底）。
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

  // ------------------------------------------------ 会话与 /api 应答
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
    if (activeSession) activeSession._emit('message', { data: JSON.stringify(obj) });
  }

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
        stage: 'release',
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

  // 与 main.py 的 LANGUAGES 保持一致（原生壳内 SenseVoice 自动识别语言，
  // 下拉只是 UI 惯性，选什么都行）。
  const LANGUAGES = [
    { code: 'auto', label: '自动识别' },
    { code: 'zh', label: '中文' },
    { code: 'en', label: 'English' },
    { code: 'ja', label: '日本語' },
    { code: 'ko', label: '한국어' },
  ];

  // ------------------------------------------------ 注册到传输层
  EchoNoteTransport.registerNative({
    /** 会话打开（app.js 刚拿到会话对象）：补发一次当前状态。 */
    onSessionOpen() {
      pushStatus(true);
    },
    /** app.js 发来的 JSON 帧（config / flush）。 */
    onJSON(text) {
      handleJson(text);
    },
    /** app.js 发来的音频帧（Int16Array.buffer）。 */
    onAudio(buffer) {
      handleAudio(buffer);
    },
    closeSession() {
      engine.endSession();
    },
    api,
  });
}
