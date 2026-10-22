/* EchoNote 声记 — frontend */
(() => {
  'use strict';

  const SAMPLE_RATE = 16000;
  const MERGE_SAMPLES = 1600; // 100 ms upstream frame (PRD D-06 / FR-038)
  const PROTOCOL_VERSION = '3';
  const RECONNECT_BASE_MS = 1500;
  const RECONNECT_MAX_MS = 15000;
  const RECONNECT_ATTEMPTS_CAP = 4;
  const CORRECTION_RATIO = 0.2;
  const CORRECTION_BADGE_MS = 8000;
  const MEMORY_BUDGET_BYTES = 200 * 1024 * 1024;
  const LOW_QUOTA_BYTES = 300 * 1024 * 1024;
  const STORAGE_WORKER_URL = 'storage-worker.js';
  // 回放按窗口解码：一次只把 30 s 的 PCM 读进内存（≈0.96 MB Int16 + 1.9 MB
  // Float32），60 min 录音也不会整段进堆。队列余量低于 5 s 就补下一块，
  // 相当于提前 25 s 排好下一段，接缝由 PlaybackScheduler 贴着上一段结尾排。
  const PLAY_WINDOW_S = 30;
  const PLAY_PREFETCH_S = 5;
  const PLAY_PUMP_MS = 500;
  const PLAY_SEEK_STEP_S = 5;

  const Expo = window.EchoNoteExport;
  const Wav = window.EchoNoteWav;
  const Audio = window.EchoNoteAudioStore;
  const Playback = window.EchoNotePlayback;

  const el = {
    recordBtn: document.getElementById('record-btn'),
    timer: document.getElementById('timer'),
    hint: document.getElementById('hint'),
    modelTag: document.getElementById('model-tag'),
    footerModel: document.getElementById('footer-model'),
    versionTag: document.getElementById('version-tag'),
    connPill: document.getElementById('conn-pill'),
    modelPill: document.getElementById('model-pill'),
    language: document.getElementById('language'),
    modelSelect: document.getElementById('model-select'),
    hotwordsInput: document.getElementById('hotwords-input'),
    waveform: document.getElementById('waveform'),
    transcript: document.getElementById('transcript'),
    emptyState: document.getElementById('empty-state'),
    finals: document.getElementById('finals'),
    partial: document.getElementById('partial'),
    partialText: document.querySelector('#partial .partial-text'),
    copyBtn: document.getElementById('copy-btn'),
    clearBtn: document.getElementById('clear-btn'),
    exportBtn: document.getElementById('export-btn'),
    exportMenu: document.getElementById('export-menu'),
    errorAlert: document.getElementById('error-alert'),
    errorText: document.getElementById('error-text'),
    retryBtn: document.getElementById('retry-btn'),
    troubleshoot: document.getElementById('troubleshoot-link'),
    notice: document.getElementById('notice'),
    noticeText: document.getElementById('notice-text'),
    noticeAction: document.getElementById('notice-action'),
    srStatus: document.getElementById('sr-status'),
    newSessionDialog: document.getElementById('new-session-dialog'),
    nsdCancel: document.getElementById('nsd-cancel'),
    nsdExport: document.getElementById('nsd-export'),
    nsdConfirm: document.getElementById('nsd-confirm'),
    modelNotice: document.getElementById('model-notice'),
    offlineBadge: document.getElementById('offline-badge'),
    offlineNote: document.getElementById('local-note-text'),
    speedNotice: document.getElementById('speed-notice'),
    playbar: document.getElementById('playbar'),
    playToggle: document.getElementById('play-toggle'),
    playSeek: document.getElementById('play-seek'),
    playTime: document.getElementById('play-time'),
  };

  const HOTWORDS_KEY = 'echonote.hotwords';

  function loadHotwords() {
    try {
      return (localStorage.getItem(HOTWORDS_KEY) || '').slice(0, 200);
    } catch {
      return ''; // private mode / storage disabled: the field still works
    }
  }

  function saveHotwords(value) {
    try {
      localStorage.setItem(HOTWORDS_KEY, value);
    } catch {
      /* nothing to do; the value lives for this page only */
    }
  }

  const state = {
    ws: null,
    // Domain words sent with every `config`. Measured on the 75 MB model:
    // 31.0% -> 10.3% character error rate on the same clip.
    hotwords: loadHotwords(),
    connected: false,
    modelReady: false,
    modelKey: '',
    modelName: '加载中',
    offlineReady: false,
    recording: false,
    recordingStartedAt: null,
    endedAt: null,
    timerId: null,
    rafId: null,
    audioCtx: null,
    mediaStream: null,
    sourceNode: null,
    workletNode: null,
    analyser: null,
    store: null,
    ledger: null,
    sessionKey: '',
    storageErrorWarned: false,
    audioBuffer: new Int16Array(0),
    finals: [],
    finalIds: new Set(),
    partial: '',
    lastPartialText: '',
    language: 'zh',
    languages: [],
    maxRecordMinutes: 90,
    epoch: 0,
    sessionOriginMs: 0,
    reconnectTimer: null,
    reconnectAttempts: 0,
    disconnectTimer: null,
    pollTimer: null,
    interrupted: false,
    limitWarned80: false,
    memoryWarned: false,
    backlogWarned: false,
    reduceMotion: false,
    dialogResolve: null,
    // 回放：一个复用的 AudioContext（暂停用 suspend，绝不 close）、一个
    // PlaybackScheduler 时间线、以及当前排在队列里的 BufferSource。
    playCtx: null,
    playSched: null,
    playState: 'idle', // idle | playing | paused | seeking
    playPos: 0, // 已排入队列的音频末端位置（秒）
    playSources: [],
    playLoad: null, // 在途的窗口读取（同一时刻只允许一笔）
    playToken: 0, // 递增即可让在途读取作废（seek / stop）
    playExhausted: false, // 已经读到文件末尾，等在播的尾巴放完
    playTimerId: null,
    playRafId: null,
    playScrub: false,
    playInteracted: false, // 最近一次指针交互发生在播放条内
    playRowId: null, // 当前高亮的字幕行 id
    playResume: false, // seek 收尾后是否继续播（供嵌套 seek 沿用）
    playSeekId: 0, // 每次 seek 的代号，晚到的 await 一看到代号变了就退出
  };

  /* ─────────────────── audio storage (OPFS / memory) ───────────────────
   * 录音不再堆在 JS 堆里：Worker 用 createSyncAccessHandle 同步写 OPFS 文件，
   * 主线程只留一个字节计数器，内存占用与录音时长无关（90 分钟 ≈ 173 MB → 常量）。
   * OPFS 不可用（隐私模式、无 Worker、句柄创建失败）时降级为内存实现并置
   * `fallback: true`，录音与导出照常可用。
   */

  /**
   * audio-store.js（`PcmLedger` + `createMemoryStore`）由 index.html 静态引入，
   * app.js 只负责接线：OPFS 落盘 + 失败降级到内存实现。
   */
  const AudioStore = {
    /**
     * 打开一段录音的落盘目标。任何失败都在内部降级为内存实现，调用方不需要
     * try/catch，也不会因为存储不可用而录不了音。
     *
     * @param {string} sessionKey 同一段录音（含连接中断后继续）复用的文件名
     * @returns {Promise<{fallback: boolean, totalBytes: number, sampleCount: number,
     *   append(int16: Int16Array): void, finish(): Promise<void>, flush(): void,
     *   read(from: number, to: number): Promise<ArrayBuffer>,
     *   toBlob(): Promise<Blob>, discard(): Promise<void>}>}
     */
    async open(sessionKey, options = {}) {
      try {
        return await openOpfsStore(sessionKey, options.onError);
      } catch (error) {
        console.warn('[echonote] 本地文件落盘不可用，改用内存存储：', error);
        return Audio.createMemoryStore();
      }
    },
  };

  async function openOpfsStore(sessionKey, onError) {
    if (!navigator.storage || typeof navigator.storage.getDirectory !== 'function') {
      throw new Error('缺少 OPFS 支持');
    }
    const root = await navigator.storage.getDirectory(); // 隐私模式下可能直接抛错
    const tmpName = `${sessionKey}.pcm.tmp`;
    const finalName = `${sessionKey}.pcm`;

    const worker = new Worker(STORAGE_WORKER_URL);
    // 请求按 id 关联，不依赖回复顺序：append 是即发即忘的，一旦按队列顺序
    // 认领回复，Worker 报错就会把一个 append 的错算到后面的 finish/read 头上。
    const pending = new Map();
    let nextId = 1;
    let bytes = 0;
    let currentName = tmpName;
    let fileHandle = null;
    let finishedHandle = null;
    let finishing = null;
    let closed = false; // 已 finish 或已失败：不再接收 append
    let failed = false; // 落盘失败：文件不可信，不可导出

    function reportError(error) {
      if (typeof onError === 'function') onError(error);
    }

    /** Worker 挂了或写盘出错：标记失败、清空等待者，之后 read/toBlob 直接报错。 */
    function failStorage(message) {
      if (failed) return;
      failed = true;
      closed = true;
      const error = new Error(message || '存储写入失败');
      pending.forEach((waiting) => waiting.reject(error));
      pending.clear();
      try {
        worker.terminate();
      } catch {
        /* 已经没了 */
      }
      reportError(error.message);
    }

    worker.addEventListener('message', (event) => {
      const message = event.data || {};
      if (message.type === 'progress') {
        bytes = message.offset; // Worker 报的已落盘字节，比本地累加更可信
        return;
      }
      if (message.type === 'flushed') {
        // 尽力而为的刷盘：失败只告警，绝不进 failStorage（会关掉整个 Worker）。
        if (!message.ok) {
          console.warn('[echonote] 刷盘失败（已忽略，录音继续）：', message.error);
        }
        return;
      }
      const waiting = pending.get(message.id);
      if (waiting) pending.delete(message.id);
      if (message.type === 'error') {
        if (waiting) waiting.reject(new Error(message.error));
        else failStorage(message.error); // 没有主的错误只能是 append 写失败
        return;
      }
      if (waiting) waiting.resolve(message);
    });

    worker.addEventListener('error', (event) => {
      failStorage(event.message || '存储 Worker 出错');
    });

    function request(payload) {
      const id = nextId;
      nextId += 1;
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        worker.postMessage({ ...payload, id });
      });
    }

    let opened;
    try {
      opened = await request({ type: 'open', name: sessionKey });
    } catch (error) {
      worker.terminate();
      throw error;
    }
    bytes = opened.offset || 0;

    async function doFinish() {
      if (closed) return;
      closed = true;
      try {
        await request({ type: 'finish' });
      } catch (error) {
        // flush/close 没走完就不能改名：半截文件一旦叫 .pcm，导出会得到坏 WAV，
        // 下次启动也不会提示未完成。保留 .pcm.tmp，并标记本段不可导出。
        failStorage(error.message);
        return;
      }
      worker.terminate();
      try {
        fileHandle = await root.getFileHandle(currentName);
        if (typeof fileHandle.move === 'function') {
          await fileHandle.move(finalName);
          currentName = finalName;
          fileHandle = await root.getFileHandle(finalName);
        }
      } catch (error) {
        // 改名失败不影响内容：继续用 .pcm.tmp 句柄读，下次启动会提示未完成。
        console.warn('[echonote] 录音文件改名失败，保留 .tmp：', error);
      }
      finishedHandle = fileHandle;
    }

    const store = {
      fallback: false,

      get totalBytes() {
        return bytes;
      },

      get sampleCount() {
        return bytes / 2;
      },

      append(int16) {
        if (!int16 || int16.length === 0 || closed) return;
        // 必须拷贝：紧随其后的 ws.send() 还要用原 buffer，转移所有权会发出空帧。
        const copy = int16.slice();
        const id = nextId;
        nextId += 1;
        bytes += copy.byteLength;
        // 带 id 但不进 pending：写盘失败时它是"无主错误"，会被当成致命错误处理。
        worker.postMessage({ type: 'append', id, buffer: copy.buffer }, [copy.buffer]);
      },

      finish() {
        if (!finishing) finishing = doFinish();
        return finishing;
      },

      /** 只 flush 不关闭：给 beforeunload 用（关句柄会让"取消离开"后的录音静默停写）。 */
      flush() {
        if (closed || failed) return;
        const id = nextId;
        nextId += 1;
        worker.postMessage({ type: 'flush', id });
      },

      async read(from, to) {
        if (finishing) await finishing;
        if (failed) throw new Error('本段录音保存失败，无法读取');
        if (finishedHandle) {
          const file = await finishedHandle.getFile();
          return file.slice(from, to).arrayBuffer();
        }
        if (closed) throw new Error('录音文件句柄已关闭，无法读取');
        return (await request({ type: 'read', from, to })).buffer;
      },

      async toBlob() {
        if (finishing) await finishing;
        if (failed) throw new Error('本段录音保存失败，无法导出');
        // File 本身就是 Blob：零拷贝，不把录音读进内存。
        if (finishedHandle) return finishedHandle.getFile();
        return new Blob([await store.read(0, bytes)]);
      },

      async discard() {
        await store.finish();
        try {
          await root.removeEntry(currentName);
        } catch {
          /* 文件已经不在了 */
        }
      },
    };

    return store;
  }

  function newSessionKey() {
    return `echonote-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  }

  /** 录音开始前的容量预检；返回需要提示的一行文字，无需提示时返回 ''。 */
  async function checkStorageQuota() {
    try {
      if (!navigator.storage || typeof navigator.storage.estimate !== 'function') return '';
      const estimate = await navigator.storage.estimate();
      const remaining = (estimate.quota || 0) - (estimate.usage || 0);
      if (remaining >= LOW_QUOTA_BYTES) return '';
      if (typeof navigator.storage.persist === 'function') await navigator.storage.persist();
      const mib = Math.max(0, Math.round(remaining / 1048576));
      return `浏览器可用存储仅剩约 ${mib} MiB，长录音可能中途写不进去，建议先导出并清理站点数据。`;
    } catch {
      return ''; // 拿不到配额就照常录音
    }
  }

  /**
   * 启动时只报告、不处理上次残留的 .pcm.tmp：不删除、不改名、不计入字节数，
   * 因为半截文件既不是可播放的录音，也可能是用户唯一的一份素材。
   */
  async function reportUnfinishedRecordings() {
    try {
      if (!navigator.storage || typeof navigator.storage.getDirectory !== 'function') return;
      const root = await navigator.storage.getDirectory();
      let bytes = 0;
      for await (const entry of root.values()) {
        if (entry.kind !== 'file' || !entry.name.endsWith('.pcm.tmp')) continue;
        bytes += (await entry.getFile()).size;
      }
      if (bytes <= 0) return;
      const minutes = Math.max(1, Math.round(bytes / 2 / SAMPLE_RATE / 60));
      showNotice(`上次有未完成的录音（约 ${minutes} 分钟），已保留为 .tmp，不参与播放与导出。`);
    } catch {
      /* OPFS 不可用：没有可提示的残留 */
    }
  }

  function onStorageError(message) {
    if (state.storageErrorWarned) return;
    state.storageErrorWarned = true;
    showNotice(`音频写入本地文件失败：${message}。已录内容可能不完整，请立即停止录音并检查磁盘空间。`);
  }

  /* ─────────────────────────── websocket ─────────────────────────── */

  function connect() {
    if (
      state.ws &&
      (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }

    setConnPill('idle', '连接中');
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}/ws`);
    ws.binaryType = 'arraybuffer';
    state.ws = ws;

    ws.addEventListener('open', () => {
      state.connected = true;
      state.reconnectAttempts = 0;
      setConnPill('ok', '已连接');
      sendConfig();
      if (state.interrupted) {
        state.interrupted = false;
        setHint('已恢复，可继续录音');
        announce('连接已恢复，可以继续录音');
      }
      updateRecordEnabled();
    });

    ws.addEventListener('message', (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      handleMessage(message);
    });

    ws.addEventListener('close', () => {
      const wasRecording = state.recording;
      state.connected = false;
      setConnPill('bad', '已断开');

      if (wasRecording) {
        // Stop capturing immediately (<=2 s) and keep everything already
        // produced; the timeline continues from where it left off.
        stopRecording({ reason: 'connection' });
        state.interrupted = true;
        showNotice('连接中断，已停止录音。已产生的文字与音频已保留，可继续录音或直接导出。', {
          actionLabel: '继续录音（新会话）',
          action: () => startRecording({ continueSession: true }),
        });
        announce('连接中断，已停止录音');

        clearTimeout(state.disconnectTimer);
        state.disconnectTimer = setTimeout(() => {
          if (!state.connected) setHint('连接中断，已停止录音 · 可继续录音或导出');
        }, 10000);
      }

      updateRecordEnabled();
      scheduleReconnect();
    });

    ws.addEventListener('error', () => {
      /* the close handler schedules the reconnect */
    });
  }

  function scheduleReconnect() {
    clearTimeout(state.reconnectTimer);
    const exponent = Math.min(state.reconnectAttempts, RECONNECT_ATTEMPTS_CAP);
    const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** exponent);
    state.reconnectAttempts += 1;
    state.reconnectTimer = setTimeout(connect, delay);
  }

  function sendConfig() {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    state.ws.send(
      JSON.stringify({
        type: 'config',
        protocol: PROTOCOL_VERSION,
        language: state.language,
        epoch: Math.max(1, state.epoch),
        origin_ms: Math.round(state.sessionOriginMs || 0),
        hotwords: state.hotwords,
      })
    );
  }

  /**
   * Start a new recording segment on the shared timeline. `epoch` is bumped
   * per recording session so `final.id` stays unique for the page lifetime
   * and `origin_ms` keeps absolute timestamps monotonic across reconnects.
   */
  function beginSession() {
    state.epoch += 1;
    state.sessionOriginMs = state.recordingStartedAt
      ? Date.now() - state.recordingStartedAt.getTime()
      : 0;
    sendConfig();
  }

  function handleMessage(message) {
    switch (message.type) {
      case 'status':
        applyStatus(message);
        break;
      case 'partial':
        if (!state.recording) return;
        state.lastPartialText = message.text || '';
        state.partial = message.text || '';
        renderPartial();
        break;
      case 'final':
        onFinal(message);
        break;
      case 'refine':
        onRefine(message);
        break;
      case 'error':
        onServerError(message);
        break;
      case 'warning':
        onServerWarning(message);
        break;
      case 'done':
        break;
      default:
        break;
    }
  }

  /* ─────────────────────────── status ─────────────────────────── */

  function modelDisplayName(model, label) {
    // The server sends a human label ("高精度"); fall back to the folder name
    // so an unrecognised custom model still shows something meaningful.
    if (label) return label;
    if (!model) return '未知';
    const parts = String(model).split(/[\\/]/);
    return parts[parts.length - 1] || model;
  }

  function setOfflineBadge(ready) {
    state.offlineReady = !!ready;
    if (!el.offlineBadge) return;
    el.offlineBadge.hidden = !ready;
    if (el.offlineNote) {
      el.offlineNote.textContent = ready
        ? '录音与识别均在本机完成，模型已缓存在磁盘，断网也能用'
        : '录音与识别均在本机完成，不上传云端，音频只留在本机浏览器存储里';
    }
  }

  /**
   * Two models are at work in v3: a fast one draws the live caption, an
   * accurate one rewrites each finished sentence. Report both honestly —
   * the number the user feels is the live decode time, not the RTF of finals.
   */
  function updateSpeedNotice(message) {
    if (!el.speedNotice) return;
    const liveMs = Number(message.live_ms);
    const value = Number(message.rtf);
    const notes = [];

    if (Number.isFinite(liveMs) && liveMs > 0) {
      if (liveMs <= 1500) {
        notes.push(`实时字幕每次解码约 ${(liveMs / 1000).toFixed(1)} 秒，边说边出字。`);
      } else {
        notes.push(
          `实时字幕每次解码约 ${(liveMs / 1000).toFixed(1)} 秒，会比说话慢一些。` +
            '装一个更小的模型（base / small）能明显改善。'
        );
      }
    }

    if (message.refine && Number.isFinite(value) && value > 1.05) {
      notes.push(
        `定稿用更大的模型，比说话慢约 ${value.toFixed(1)} 倍；` +
          '它只在每句结束后后台运行，不影响实时字幕。'
      );
    }

    // Picking a model *smaller* than the live one used to let it rewrite the
    // caption into something worse, and the row still said 已精修. The server
    // now skips that pass; say why, otherwise the dropdown looks broken.
    if (message.refine_blocked === 'final_model_not_bigger') {
      notes.push(
        '所选的「识别模型」比实时字幕用的模型更小，精修只会把文字改差，已跳过。' +
          '想要更准，请选「快速」或更大的档位。'
      );
    }

    if (notes.length === 0) {
      el.speedNotice.hidden = true;
      el.speedNotice.textContent = '';
      return;
    }
    el.speedNotice.hidden = false;
    el.speedNotice.textContent = notes.join(' ');
  }

  /** "实时 极速 · 定稿 快速" — only when the two roles really differ. */
  function modelRoleText(message) {
    const live = modelDisplayName(message.live_model, message.live_model_label);
    const final = modelDisplayName(message.model, message.model_label);
    if (!message.live_separate) return final;
    return `实时 ${live} · 定稿 ${final}`;
  }

  function applyStatus(message) {
    if (message.protocol && message.protocol !== PROTOCOL_VERSION) {
      showError('页面与服务端版本不一致，请刷新页面。');
      return;
    }

    state.modelReady = !!message.ready;
    state.modelName = modelDisplayName(message.model, message.model_label);
    el.modelTag.textContent = state.modelReady
      ? modelRoleText(message)
      : state.modelName;
    el.footerModel.textContent = modelRoleText(message);
    setOfflineBadge(message.offline_ready);
    updateSpeedNotice(message);

    if (message.error) {
      setModelPill('bad', '模型加载失败');
      showError(`模型加载失败：${message.error}`, {
        retry: true,
        troubleshoot: !!message.auto_retry_exhausted,
      });
      setHint('模型加载失败 · 可点“重试”，无需重启程序');
    } else if (message.retrying) {
      setModelPill('idle', '正在重试');
      setHint(`正在重试（第 ${Math.max(1, message.retry_count || 1)} 次）…`);
      clearError();
    } else if (message.ready) {
      setModelPill('ok', '模型就绪');
      clearError();
      if (el.modelNotice) el.modelNotice.hidden = true;
      // Don't stomp on "已停止 · 可导出" every poll once a take exists.
      if (!state.recording && !state.endedAt) setHint('点击开始录音');
    } else {
      setModelPill('idle', '模型加载中');
      setHint('正在准备模型…');
      if (el.modelNotice) el.modelNotice.hidden = false;
    }

    updateRecordEnabled();
  }

  function updateRecordEnabled() {
    const canRecord = state.modelReady && state.connected;
    el.recordBtn.disabled = !canRecord && !state.recording;
    el.language.disabled = !state.modelReady;
    if (el.modelSelect) el.modelSelect.disabled = !state.modelReady || state.recording;
  }

  async function pollStatus() {
    try {
      const res = await fetch('/api/ready', { cache: 'no-store' });
      if (res.ok) applyStatus(await res.json());
    } catch {
      /* server not up yet */
    }
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(pollStatus, state.modelReady ? 15000 : 2000);
  }

  async function retryModel() {
    setHint('正在重试加载模型…');
    announce('正在重试加载模型');
    try {
      const res = await fetch('/api/retry', { method: 'POST' });
      if (res.ok) applyStatus(await res.json());
    } catch {
      setHint('重试请求失败，请确认服务仍在运行');
    }
  }

  async function loadConfig() {
    try {
      const res = await fetch('/api/config', { cache: 'no-store' });
      if (!res.ok) return;
      const config = await res.json();

      state.modelName = modelDisplayName(config.model, config.model_label);
      el.modelTag.textContent = state.modelName;
      el.footerModel.textContent = state.modelName;

      // The server ships a starter glossary (the app's own vocabulary). Use it
      // until the user types their own, so a first run does not look like it
      // has a broken glossary field.
      if (el.hotwordsInput && !state.hotwords && config.hotwords) {
        state.hotwords = String(config.hotwords);
        el.hotwordsInput.value = state.hotwords;
      }
      setOfflineBadge(config.offline_ready);
      if (el.versionTag && config.version) el.versionTag.textContent = `v${config.version}`;

      state.languages = config.languages || [];
      el.language.textContent = '';
      state.languages.forEach((lang) => {
        const option = document.createElement('option');
        option.value = lang.code;
        option.textContent = lang.label;
        el.language.appendChild(option);
      });

      const defaultLanguage = config.language || 'zh';
      el.language.value = defaultLanguage;
      state.language = el.language.value;

      state.maxRecordMinutes = Number(config.max_record_minutes) || 0;
      if (state.maxRecordMinutes === 0) {
        showNotice('已关闭录音时长上限（时长不限，内存风险自负）。');
      }

      // The first config message must already carry the server default
      // language (FR-006 AC5), so this awaits before connect().
      sendConfig();
      loadModels();
    } catch {
      /* defaults stay */
    }
  }

  /* ───────────────────────── model selection ───────────────────────── */

  function formatSize(mb) {
    const value = Number(mb) || 0;
    return value >= 1024 ? `${(value / 1024).toFixed(1)} GB` : `${value} MB`;
  }

  async function loadModels() {
    if (!el.modelSelect) return;
    try {
      const res = await fetch('/api/models', { cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();
      const items = data.items || [];
      const installed = items.filter((item) => item.installed);

      el.modelSelect.textContent = '';
      items.forEach((item) => {
        const option = document.createElement('option');
        option.value = item.key;
        option.disabled = !item.installed;
        option.textContent = item.installed
          ? `${item.label}（${item.key}）· ${formatSize(item.size_mb)}`
          : `${item.label}（${item.key}）· 未安装`;
        option.title = item.note || '';
        el.modelSelect.appendChild(option);
      });
      el.modelSelect.value = data.current;
      state.modelKey = data.current;
      state.modelsInstalled = installed.length;

      // Always visible: showing the entries that are *not* installed is how a
      // user discovers that a faster model exists and how to get it.
      const field = el.modelSelect.closest('.field');
      if (field) field.hidden = items.length === 0;
    } catch {
      /* leave the control disabled */
    }
  }

  async function switchModel(key) {
    if (!key || key === state.modelKey || state.recording) return;
    setHint('正在切换模型…');
    announce('正在切换识别模型');
    try {
      const res = await fetch('/api/model', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key }),
      });
      const data = await res.json();
      if (!data.ok) {
        showError(`切换失败：${data.error || '未知原因'}`);
        el.modelSelect.value = state.modelKey;
        return;
      }
      state.modelKey = data.key;
      state.modelReady = false;
      clearError();
      pollStatus();
    } catch {
      showError('切换请求失败，请确认服务仍在运行');
      el.modelSelect.value = state.modelKey;
    }
  }

  /* ─────────────────────────── recording ─────────────────────────── */

  async function toggleRecording() {
    if (state.recording) {
      stopRecording({ reason: 'user' });
    } else {
      await startRecording();
    }
  }

  async function startRecording(options = {}) {
    const continueSession = !!options.continueSession;
    if (state.recording) return;
    // 一边放一边录会把回放声录进麦克风；开录一律先停回放。
    stopPlayback();

    if (!state.connected) {
      setHint('正在连接服务器…');
      connect();
      return;
    }
    if (!state.modelReady) {
      setHint('模型还在加载，请稍候…');
      return;
    }

    const hasData = state.finals.length > 0 || (state.ledger ? state.ledger.sampleCount > 0 : false);
    if (!continueSession && hasData) {
      const confirmed = await askNewSession();
      if (!confirmed) return;
      await resetSession();
    }

    if (!Audio) {
      showError('音频存储模块加载失败，请刷新页面重试');
      return;
    }
    if (!state.ledger || !continueSession) {
      state.ledger = Audio.PcmLedger.create({ maxBytes: MEMORY_BUDGET_BYTES });
    }

    const storageNotices = [];
    const quotaNotice = await checkStorageQuota();
    if (quotaNotice) storageNotices.push(quotaNotice);

    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
    } catch (error) {
      showError(`无法访问麦克风：${friendlyMediaError(error)}`);
      setHint('麦克风不可用，请在系统设置中允许麦克风');
      announce('麦克风不可用');
      await releaseStore(); // 不留悬挂的 Worker / 句柄
      return;
    }

    // 同一段录音（连接中断后继续）复用同一个文件和会话键，WAV 才不会断成两截。
    if (continueSession) {
      if (state.store && !state.store.fallback) {
        await state.store.finish(); // 等上一段落盘改名，Worker 会接着往后写
        state.store = null;
      }
    } else {
      if (state.store) {
        await state.store.discard(); // 上一段没有任何音频，直接丢掉空文件
        state.store = null;
      }
      state.sessionKey = newSessionKey();
    }
    if (!state.store) {
      if (!state.sessionKey) state.sessionKey = newSessionKey();
      state.store = await AudioStore.open(state.sessionKey, { onError: onStorageError });
      if (state.store.fallback) {
        storageNotices.push(
          '浏览器不支持把录音写入本地文件，本次录音只暂存内存，长录音会占用较多内存。'
        );
      }
    }
    state.storageErrorWarned = false;

    try {
      const ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
      state.audioCtx = ctx;
      if (ctx.state === 'suspended') await ctx.resume();
      await ctx.audioWorklet.addModule('pcm-worklet.js');

      const source = ctx.createMediaStreamSource(stream);
      const worklet = new AudioWorkletNode(ctx, 'pcm16');
      const mute = ctx.createGain();
      mute.gain.value = 0;
      worklet.connect(mute);
      mute.connect(ctx.destination);
      source.connect(worklet);

      const analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      analyser.smoothingTimeConstant = 0.78;
      source.connect(analyser);

      worklet.port.onmessage = (event) => handlePcm(event.data);

      // Device unplugged / permission revoked mid-recording (FR-041).
      stream.getTracks().forEach((track) => {
        track.addEventListener('ended', () => {
          if (!state.recording) return;
          stopRecording({ reason: 'device' });
          showNotice('麦克风已断开，已停止录音。已产生的文字与音频已保留，可导出。');
          announce('麦克风已断开，已停止录音');
        });
      });

      state.sourceNode = source;
      state.workletNode = worklet;
      state.analyser = analyser;
      state.mediaStream = stream;

      if (!continueSession || !state.recordingStartedAt) {
        state.recordingStartedAt = new Date();
        state.endedAt = null;
        state.limitWarned80 = false;
        state.memoryWarned = false;
        state.backlogWarned = false;
      }

      beginSession();

      state.recording = true;
      el.recordBtn.classList.add('recording');
      el.recordBtn.setAttribute('aria-label', '停止录音');
      el.recordBtn.disabled = false;
      clearNotice();
      if (storageNotices.length) showNotice(storageNotices.join(' '));
      clearError();
      setHint('正在录音 · 实时转写中');
      startTimer();
      drawWaveform();
      updateRecordEnabled();
      // 录音中回放不可用：让播放条跟着录音状态变灰（updateExportState 里同步）。
      updateExportState();
      announce('开始录音');
    } catch (error) {
      console.error(error);
      showError(`音频初始化失败：${error.message || error.name}`);
      setHint('音频初始化失败，请刷新页面重试');
      cleanupAudio();
      await releaseStore(); // 同上：早退不能把句柄留在半空
    }
  }

  function stopRecording(options = {}) {
    if (!state.recording) return;
    const reason = options.reason || 'user';

    state.recording = false;
    el.recordBtn.classList.remove('recording');
    el.recordBtn.setAttribute('aria-label', '开始录音');

    flushAudioBuffer();
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify({ type: 'flush' }));
    }
    // 收尾落盘（flush + close 后把 .tmp 改名成 .pcm）；不 await——导出与读取
    // 会等同一个 promise，UI 不必挂在这里。一声没录到就删掉空文件，
    // 免得留下永远无人提示、也永远没人清理的 0 字节 .pcm（A8）。
    if (state.store) {
      if (state.ledger && state.ledger.sampleCount > 0) {
        state.store.finish();
      } else {
        state.store.discard();
        state.store = null; // 空文件已删，续录时重新打开，避免往废弃实例里写
      }
    }

    state.endedAt = new Date();
    stopTimer();
    cancelAnimationFrame(state.rafId);
    state.rafId = null;
    cleanupAudio();
    drawIdleWaveform();

    el.timer.textContent = Expo.formatClock(durationMs() / 1000);

    if (reason === 'user') {
      setHint('已停止 · 正在生成最后一段…');
      announce('已停止录音');
    } else if (reason === 'limit') {
      setHint('已达录音时长上限，已自动停止');
    }

    updateRecordEnabled();
    updateExportState();
  }

  function durationMs() {
    if (!state.recordingStartedAt) return 0;
    const end = state.recording ? Date.now() : (state.endedAt || state.recordingStartedAt).getTime();
    return Math.max(0, end - state.recordingStartedAt.getTime());
  }

  /**
   * 录音没真正开始就退出（拿不到麦克风 / AudioContext 起不来）时调用：
   * 有音频就 finish 保下来（续录场景），一声没有就 discard 掉空文件，
   * 两种情况都不留下悬挂的 Worker 与同步句柄。
   */
  async function releaseStore() {
    const released = state.store;
    state.store = null;
    if (!released) return;
    try {
      if (state.ledger && state.ledger.sampleCount > 0) await released.finish();
      else await released.discard();
    } catch (error) {
      console.warn('[echonote] 释放录音文件失败：', error);
    }
  }

  async function resetSession() {
    // 先把回放停掉：在途的 read 不能落在即将被 discard 的 store 上。
    stopPlayback();
    const discarded = state.store;
    state.store = null;
    state.ledger = null;
    state.sessionKey = '';
    state.storageErrorWarned = false;
    if (discarded) {
      try {
        await discarded.discard(); // 用户已确认「同时清空录音音频」：文件一并删掉
      } catch (error) {
        console.warn('[echonote] 清理旧录音文件失败：', error);
      }
    }
    state.audioBuffer = new Int16Array(0);
    state.finals = [];
    state.finalIds.clear();
    state.partial = '';
    state.lastPartialText = '';
    state.limitWarned80 = false;
    state.memoryWarned = false;
    state.backlogWarned = false;
    el.finals.textContent = '';
    el.emptyState.hidden = false;
    renderPartial();
    el.timer.textContent = '00:00';
    updateExportState();
  }

  function cleanupAudio() {
    try {
      if (state.workletNode) state.workletNode.port.onmessage = null;
      if (state.workletNode) state.workletNode.disconnect();
      if (state.sourceNode) state.sourceNode.disconnect();
      if (state.mediaStream) state.mediaStream.getTracks().forEach((track) => track.stop());
      if (state.audioCtx && state.audioCtx.state !== 'closed') state.audioCtx.close();
    } catch {
      /* ignore teardown errors */
    }
    state.workletNode = null;
    state.sourceNode = null;
    state.mediaStream = null;
    state.audioCtx = null;
    state.analyser = null;
  }

  function friendlyMediaError(error) {
    if (!error) return '未知错误';
    if (error.name === 'NotAllowedError') return '请允许浏览器使用麦克风';
    if (error.name === 'NotFoundError') return '没有找到麦克风设备';
    if (error.name === 'NotReadableError') return '麦克风被其他程序占用';
    return error.message || error.name;
  }

  /* ─────────────────────────── audio pipeline ─────────────────────────── */

  /**
   * Merge worklet chunks into >=100 ms frames before sending. The worklet
   * posts every 8 ms at 16 kHz, so without this the socket would carry up
   * to 125 frames/second (PRD FR-038).
   */
  function handlePcm(buffer) {
    if (!state.recording) return;
    const incoming = new Int16Array(buffer);
    if (incoming.length === 0) return;

    const merged = new Int16Array(state.audioBuffer.length + incoming.length);
    merged.set(state.audioBuffer, 0);
    merged.set(incoming, state.audioBuffer.length);

    let offset = 0;
    while (merged.length - offset >= MERGE_SAMPLES) {
      sendFrame(merged.slice(offset, offset + MERGE_SAMPLES));
      offset += MERGE_SAMPLES;
    }
    state.audioBuffer = merged.slice(offset);
  }

  function sendFrame(frame) {
    // 记账永远成功（PcmLedger 不丢音频），落盘与转发都不受预算影响。
    state.ledger.accept(frame);
    state.store.append(frame);
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(frame.buffer);
    }
    checkMemory();
  }

  function flushAudioBuffer() {
    const pending = state.audioBuffer;
    state.audioBuffer = new Int16Array(0);
    if (pending.length >= 160) sendFrame(pending);
  }

  function checkMemory() {
    if (state.memoryWarned || !state.ledger.overBudget) return;
    state.memoryWarned = true;
    const mib = Math.round(state.ledger.byteLength / 1048576);
    showNotice(`本次录音已超过约 ${mib} MiB，建议先导出，再开始新录音。`);
  }

  /* ─────────────────────────── timer & limits ─────────────────────────── */

  function startTimer() {
    stopTimer();
    const tick = () => {
      const elapsed = durationMs();
      el.timer.textContent = Expo.formatClock(elapsed / 1000);
      if (state.recording) checkLimits(elapsed);
    };
    tick();
    state.timerId = setInterval(tick, 250);
  }

  function stopTimer() {
    if (state.timerId) {
      clearInterval(state.timerId);
      state.timerId = null;
    }
  }

  function checkLimits(elapsedMs) {
    const maxMs = state.maxRecordMinutes > 0 ? state.maxRecordMinutes * 60000 : 0;
    if (maxMs > 0) {
      if (elapsedMs >= maxMs) {
        stopRecording({ reason: 'limit' });
        showNotice(
          `已达 ${state.maxRecordMinutes} 分钟录音上限，已自动停止。已产生的文字与音频可正常导出。`
        );
        announce('已达录音时长上限，已停止录音');
        return;
      }
      if (!state.limitWarned80 && elapsedMs >= maxMs * 0.8) {
        state.limitWarned80 = true;
        const remaining = Math.max(1, Math.ceil((maxMs - elapsedMs) / 60000));
        showNotice(`剩余约 ${remaining} 分钟，建议先导出当前记录。`);
        announce(`剩余约 ${remaining} 分钟`);
      }
    }
  }

  /* ─────────────────────────── waveform ─────────────────────────── */

  function sizeCanvas() {
    const dpr = window.devicePixelRatio || 1;
    const rect = el.waveform.getBoundingClientRect();
    const width = Math.max(1, Math.floor(rect.width * dpr));
    const height = Math.max(1, Math.floor(rect.height * dpr));
    if (el.waveform.width !== width || el.waveform.height !== height) {
      el.waveform.width = width;
      el.waveform.height = height;
    }
    return { dpr, width, height };
  }

  function drawIdleWaveform() {
    const { dpr, width, height } = sizeCanvas();
    const ctx = el.waveform.getContext('2d');
    ctx.clearRect(0, 0, width, height);

    const middle = height / 2;
    ctx.strokeStyle = 'rgba(100, 255, 218, 0.38)';
    ctx.lineWidth = 2 * dpr;
    ctx.beginPath();
    ctx.moveTo(0, middle);
    ctx.lineTo(width, middle);
    ctx.stroke();

    ctx.fillStyle = 'rgba(100, 255, 218, 0.35)';
    const step = width / 28;
    for (let i = 1; i < 28; i += 1) {
      const x = i * step;
      const amp = (i % 3 === 0 ? 6 : 3) * dpr;
      ctx.fillRect(x, middle - amp, Math.max(1, dpr), amp * 2);
    }
  }

  function drawWaveform() {
    if (!state.recording || !state.analyser) return;

    const { dpr, width, height } = sizeCanvas();
    const ctx = el.waveform.getContext('2d');
    const data = new Uint8Array(state.analyser.frequencyBinCount);
    state.analyser.getByteTimeDomainData(data);

    ctx.clearRect(0, 0, width, height);

    const gradient = ctx.createLinearGradient(0, 0, width, 0);
    gradient.addColorStop(0, 'rgba(100, 255, 218, 0.25)');
    gradient.addColorStop(0.5, 'rgba(100, 255, 218, 0.95)');
    gradient.addColorStop(1, 'rgba(100, 255, 218, 0.25)');

    ctx.strokeStyle = gradient;
    ctx.lineWidth = 2 * dpr;
    ctx.beginPath();
    const step = width / data.length;
    for (let i = 0; i < data.length; i += 1) {
      const value = data[i] / 128 - 1;
      const x = i * step;
      const y = height / 2 + value * (height * 0.34);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();

    // Respect prefers-reduced-motion: draw a single frozen frame instead of
    // animating every cell (PRD §10.2, promoted to P1).
    if (state.reduceMotion) return;
    state.rafId = requestAnimationFrame(drawWaveform);
  }

  /* ─────────────────────────── transcript ─────────────────────────── */

  function onFinal(message) {
    if (!message.id || state.finalIds.has(message.id)) return; // FR-010 AC5
    state.finalIds.add(message.id);

    const originSeconds = state.sessionOriginMs / 1000;
    const item = {
      id: message.id,
      text: message.text,
      start: originSeconds + Number(message.start || 0),
      end: originSeconds + Number(message.end || 0),
      epoch: message.epoch,
      corrected: isCorrected(message.text, state.lastPartialText),
      // v3: the live model produced this text; the accurate model may still
      // replace it via a `refine` message with the same id.
      stable: message.stable !== false,
    };

    state.finals.push(item);
    state.partial = '';
    state.lastPartialText = '';
    appendFinal(item);
    renderPartial();
    updateExportState();

    if (state.recording) setHint('正在录音 · 实时转写中');
    else setHint('已停止 · 可导出');
    announce(item.corrected ? '新增一条字幕（已修正）' : '新增一条字幕');
  }

  /**
   * Replace the text of a final that was emitted with `stable: false`.
   * The row keeps its position and timestamp; only the words change, so a
   * reader never sees the transcript jump around.
   */
  function onRefine(message) {
    const item = state.finals.find((entry) => entry.id === message.id);
    if (!item) return;

    const row = findRow(item.id);
    const next = String(message.text || '').trim();
    const changed = next && next !== item.text;

    if (changed) {
      item.text = next;
      const text = row && row.querySelector('p');
      if (text) text.textContent = next;
    }
    item.stable = true;

    if (row) {
      if (changed) setRowBadge(row, '已精修', true);
      else setRowBadge(row, '', false);
    }
    updateExportState();
  }

  function findRow(id) {
    for (const row of el.finals.children) {
      if (row.dataset && row.dataset.id === id) return row;
    }
    return null;
  }

  /**
   * One badge slot per row: "精修中" while the accurate pass runs, "已精修"
   * once it lands and changed the text, nothing otherwise.
   */
  function setRowBadge(row, label, fade) {
    let badge = row.querySelector('.badge');
    if (!label) {
      if (badge) badge.remove();
      return;
    }
    if (!badge) {
      badge = document.createElement('span');
      badge.className = 'badge';
      row.appendChild(badge);
    }
    badge.textContent = label;
    badge.classList.remove('fade');
    badge.classList.toggle('pending', label === '精修中');
    if (fade) {
      setTimeout(() => badge.classList.add('fade'), CORRECTION_BADGE_MS);
    }
  }

  function appendFinal(item) {
    el.emptyState.hidden = true;

    const row = document.createElement('li');
    row.className = 'segment';
    row.dataset.id = item.id;

    const ts = document.createElement('span');
    ts.className = 'ts';
    ts.textContent = Expo.formatClock(item.start);
    ts.setAttribute('aria-hidden', 'true');

    const text = document.createElement('p');
    text.textContent = item.text;

    row.appendChild(ts);
    row.appendChild(text);
    el.finals.appendChild(row);

    if (!item.stable) {
      // Accurate pass still running: say so instead of pretending this is
      // the last word on the sentence.
      setRowBadge(row, '精修中', false);
    } else if (item.corrected) {
      setRowBadge(row, '已修正', true);
    }

    scrollToBottom();
  }

  function renderPartial() {
    if (state.partial) {
      el.partial.hidden = false;
      el.partialText.textContent = state.partial;
      scrollToBottom();
    } else {
      el.partial.hidden = true;
      el.partialText.textContent = '';
    }
  }

  function scrollToBottom() {
    const box = el.transcript;
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 160;
    if (nearBottom) box.scrollTop = box.scrollHeight;
  }

  function clearTranscript(ask = true) {
    const message = '清空当前转写文本？录音音频会保留，仍可导出 WAV。';
    if (ask && state.finals.length > 0 && !window.confirm(message)) return;
    state.finals = [];
    state.finalIds.clear();
    state.partial = '';
    state.lastPartialText = '';
    state.playRowId = null; // 行都没了，高亮记账一起清掉
    el.finals.textContent = '';
    el.emptyState.hidden = false;
    renderPartial();
    updateExportState();
    announce('转写文本已清空，录音音频保留');
  }

  /* ─────────────────────────── correction detection ─────────────────────────── */

  function normalizeText(text) {
    return String(text || '')
      .replace(/[\s，。！？、,.!?;；:"'“”‘’()（）]/g, '')
      .toLowerCase();
  }

  function editDistance(a, b) {
    if (!a.length) return b.length;
    if (!b.length) return a.length;
    let previous = Array.from({ length: b.length + 1 }, (_, index) => index);
    for (let i = 1; i <= a.length; i += 1) {
      const current = [i];
      for (let j = 1; j <= b.length; j += 1) {
        const cost = a[i - 1] === b[j - 1] ? 0 : 1;
        current[j] = Math.min(current[j - 1] + 1, previous[j] + 1, previous[j - 1] + cost);
      }
      previous = current;
    }
    return previous[b.length];
  }

  function isCorrected(finalText, partialText) {
    const a = normalizeText(finalText);
    const b = normalizeText(partialText);
    if (!a || !b) return false;
    return editDistance(a, b) / Math.max(a.length, b.length) > CORRECTION_RATIO;
  }

  /* ─────────────────────────── export ─────────────────────────── */

  function buildMeta() {
    return Expo.buildMetadata({
      startedAt: state.recordingStartedAt || new Date(),
      endedAt: state.recording ? new Date() : state.endedAt || state.recordingStartedAt || new Date(),
      model: state.modelName,
      language: state.language,
      languages: state.languages,
    });
  }

  function updateExportState() {
    const hasText = state.finals.length > 0;
    const hasAudio = !!state.ledger && state.ledger.sampleCount > 0;

    el.copyBtn.disabled = !hasText;
    el.clearBtn.disabled = !hasText && !state.partial;

    el.exportMenu.querySelectorAll('[role="menuitem"]').forEach((button) => {
      const format = button.dataset.format;
      button.disabled = format === 'wav' ? !hasAudio : !hasText;
    });

    syncPlaybar();
  }

  async function exportAs(format) {
    closeMenu(false);
    const meta = buildMeta();

    if (format === 'wav') {
      if (!state.store || !state.ledger || state.ledger.sampleCount === 0) return;
      if (state.store.fallback) {
        // 内存降级模式：toBlob() 要重建一份 PCM，导出峰值确实是 2×。
        const peakMib = Math.ceil((state.ledger.byteLength * 2) / 1048576);
        setHint(`正在导出 WAV，预计占用约 ${peakMib} MiB 内存…`);
      } else {
        setHint('正在导出 WAV…');
      }
      try {
        // 44 字节头 + PCM Blob 直接拼。仅在 OPFS 落盘模式下 PCM 从不读进内存，
        // 峰值才与录音时长无关（PRD PR-09）；内存降级模式仍是 2×。
        const pcm = await state.store.toBlob();
        const header = Wav.buildWavHeader(state.ledger.sampleCount, SAMPLE_RATE);
        const blob = Wav.buildWavBlobFromParts(header, pcm);
        download(blob, Expo.fileName(meta, 'wav'), 'audio/wav');
      } catch (error) {
        showError(`导出 WAV 失败：${error.message || error}`);
        setHint('导出失败，可稍后重试');
        return;
      }
      setHint('WAV 已开始下载');
      announce('WAV 已开始下载');
      return;
    }

    if (state.finals.length === 0) return;

    if (format === 'txt') {
      download(
        new Blob([Expo.buildTxt(meta, state.finals)], { type: 'text/plain;charset=utf-8' }),
        Expo.fileName(meta, 'txt')
      );
    } else if (format === 'md') {
      download(
        new Blob([Expo.buildMarkdown(meta, state.finals)], { type: 'text/markdown;charset=utf-8' }),
        Expo.fileName(meta, 'md')
      );
    } else if (format === 'srt') {
      download(
        new Blob([Expo.buildSrt(state.finals)], { type: 'application/x-subrip;charset=utf-8' }),
        Expo.fileName(meta, 'srt')
      );
    }
    setHint('已开始下载');
    announce('导出已开始');
  }

  function download(blob, name, type) {
    const typed = type && blob.type !== type ? new Blob([blob], { type }) : blob;
    const url = URL.createObjectURL(typed);
    const link = document.createElement('a');
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  /* ─────────────────────────── export menu (APG) ─────────────────────────── */

  function menuItems() {
    return Array.from(el.exportMenu.querySelectorAll('[role="menuitem"]'));
  }

  function enabledItems() {
    return menuItems().filter((item) => !item.disabled);
  }

  function openMenu() {
    el.exportMenu.hidden = false;
    el.exportBtn.setAttribute('aria-expanded', 'true');
    const items = enabledItems();
    if (items.length) {
      items.forEach((item) => {
        item.tabIndex = -1;
      });
      items[0].tabIndex = 0;
      items[0].focus();
    }
  }

  function closeMenu(returnFocus) {
    if (el.exportMenu.hidden) return;
    el.exportMenu.hidden = true;
    el.exportBtn.setAttribute('aria-expanded', 'false');
    if (returnFocus) el.exportBtn.focus();
  }

  /* ─────────────────────────── playback ───────────────────────────
   * 回放只做三件事：把某个位置换算成一段字节、把这段字节排进 AudioContext 的
   * 时间线、把时间线换算回进度条。解码永远是「按 30 s 窗口」的：整段录音从不
   * 进内存（60 min ≈ 180 MB PCM → 峰值 ≈ 3 MB）。
   *
   * 状态机：idle →(播放)→ playing →(暂停)→ paused →(播放)→ playing
   *        任意态 →(seek)→ seeking → playing / paused
   *        playing →(队列放空且无更多数据)→ idle（进度条停在末尾）
   */

  /** 录音总时长（秒）。没有音频、没有存储实例或 playback.js 缺失时返回 0。 */
  function playDurationS() {
    if (!Playback || !state.store || !state.ledger) return 0;
    return state.ledger.sampleCount / SAMPLE_RATE;
  }

  /** 整页只建一个 AudioContext：暂停用 suspend，重播用 resume（close 会带来冷启动爆音）。 */
  function ensurePlayCtx() {
    if (state.playCtx) return state.playCtx;
    if (!Playback) return null;
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return null;
    state.playCtx = new Ctor();
    state.playSched = Playback.PlaybackScheduler.create({ leadS: 0.02 });
    return state.playCtx;
  }

  /** 当前播放头位置：已排入队列的末端位置减去队列里还没放完的时长。 */
  function playPosition() {
    const ctx = state.playCtx;
    const sched = state.playSched;
    const duration = playDurationS();
    if (!ctx || !sched || duration <= 0) return 0;
    const ahead = Math.max(0, sched.nextTime - ctx.currentTime);
    return Math.max(0, Math.min(duration, state.playPos - ahead));
  }

  function renderPlayTime(seconds) {
    const label = `${Expo.formatClock(seconds)} / ${Expo.formatClock(playDurationS())}`;
    // 每帧都会走到这里：只在文字真的变了时才写 DOM。
    if (el.playTime && el.playTime.textContent !== label) el.playTime.textContent = label;
    if (el.playSeek) el.playSeek.setAttribute('aria-valuetext', Expo.formatClock(seconds));
  }

  function updatePlayUI() {
    if (!el.playToggle) return;
    const playing = state.playState === 'playing';
    const pos = state.playScrub ? Number(el.playSeek.value) : playPosition();
    const label = playing ? '暂停' : '播放';
    if (el.playToggle.textContent !== label) {
      el.playToggle.textContent = label;
      el.playToggle.setAttribute('aria-label', playing ? '暂停播放' : '播放录音');
    }
    el.playbar.classList.toggle('is-playing', playing);
    if (!state.playScrub) {
      // 滑块步长 0.1，先自己取整再比，避免每帧都写一次 value。
      const shown = String(Math.round(pos * 10) / 10);
      if (el.playSeek.value !== shown) el.playSeek.value = shown;
    }
    renderPlayTime(pos);
  }

  /** 音频可用性变了才动 max / disabled：录音中、清空后、首次录到声音都要过这里。 */
  function syncPlaybar() {
    if (!el.playToggle || !el.playSeek) return;
    const duration = playDurationS();
    const usable = duration > 0 && !state.recording;
    el.playToggle.disabled = !usable;
    el.playSeek.disabled = !usable;
    const max = usable ? duration.toFixed(1) : '0';
    if (el.playSeek.max !== max) el.playSeek.max = max;
    if (!usable && state.playState !== 'idle') stopPlayback();
    updatePlayUI();
  }

  function startLoops() {
    stopLoops();
    state.playTimerId = setInterval(pump, PLAY_PUMP_MS);
    state.playRafId = requestAnimationFrame(tick);
  }

  function stopLoops() {
    if (state.playTimerId) {
      clearInterval(state.playTimerId);
      state.playTimerId = null;
    }
    if (state.playRafId) {
      cancelAnimationFrame(state.playRafId);
      state.playRafId = null;
    }
  }

  function tick() {
    state.playRafId = requestAnimationFrame(tick);
    if (state.playState !== 'playing') return;
    if (maybeFinishPlayback()) return;
    updatePlayUI();
  }

  function stopSources() {
    const list = state.playSources;
    state.playSources = [];
    list.forEach((source) => {
      source.onended = null; // 别让已经停掉的节点回头改数组
      try {
        source.stop();
      } catch {
        /* 可能已经自然结束 */
      }
      try {
        source.disconnect();
      } catch {
        /* 同上 */
      }
    });
  }

  /** Int16 PCM → 归一化到 [-1, 1) 的 Float32 声道。 */
  function pcmToChannel(pcm, channel) {
    for (let i = 0; i < pcm.length; i += 1) channel[i] = pcm[i] / 32768;
  }

  /**
   * 读并排入**一个** 30 s 窗口。返回 'ok' | 'end' | 'stale' | 'error'。
   *
   * 空窗口（fromByte === toByte）只表示「没有更多数据了」：此刻队列里可能还有
   * 最长 30 s 的尾巴在放，所以只置 playExhausted，由 pump 等队列放空后再收尾。
   */
  async function loadNextWindow() {
    const ctx = state.playCtx;
    const sched = state.playSched;
    const store = state.store;
    if (!ctx || !sched || !store) return 'error';

    const token = state.playToken;
    try {
      const win = Playback.windowFor({
        positionS: state.playPos,
        windowS: PLAY_WINDOW_S,
        sampleRate: SAMPLE_RATE,
        totalBytes: store.totalBytes,
      });

      // 空窗口就是「播到结尾」：不抛错、不重试，等尾巴放完由 pump 收尾。
      if (win.fromByte === win.toByte) {
        state.playExhausted = true;
        return 'end';
      }

      const raw = await store.read(win.fromByte, win.toByte);
      if (token !== state.playToken) return 'stale'; // seek/停止期间读完的，丢弃

      const pcm = new Int16Array(raw);
      if (pcm.length === 0) {
        state.playExhausted = true;
        return 'end';
      }

      const buffer = ctx.createBuffer(1, pcm.length, SAMPLE_RATE);
      pcmToChannel(pcm, buffer.getChannelData(0));

      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(ctx.destination);
      source.onended = () => {
        state.playSources = state.playSources.filter((item) => item !== source);
        try {
          source.disconnect(); // 放完就摘掉，别让已结束的节点挂着 destination
        } catch {
          /* 已经断开过了 */
        }
      };

      const startAt = sched.nextStartAt(ctx.currentTime);
      source.start(startAt);
      sched.advance(startAt, buffer.duration);
      state.playSources.push(source);
      state.playPos += buffer.duration;
      state.playExhausted = false;
      return 'ok';
    } catch (error) {
      // 读不出来（文件被丢弃、Worker 出错）就停在当前位置，不跳到末尾也不重试。
      console.warn('[echonote] 回放读取失败：', error);
      stopAt(playPosition());
      return 'error';
    }
  }

  /** 同一时刻只允许一笔窗口读取：并发读同一段既浪费又会让时间线重叠。 */
  function requestWindow() {
    if (state.playLoad) return state.playLoad;
    const task = loadNextWindow();
    state.playLoad = task;
    const done = () => {
      if (state.playLoad === task) state.playLoad = null;
    };
    task.then(done, done);
    return task;
  }

  /** 队列余量不足 5 s 就补一块；已到末尾则等尾巴放完再收尾。 */
  function pump() {
    if (state.playState !== 'playing') return;
    if (maybeFinishPlayback()) return;
    if (state.playExhausted) return;
    const ctx = state.playCtx;
    const sched = state.playSched;
    if (!ctx || !sched) return;
    if (state.playLoad) return;
    if (sched.nextTime - ctx.currentTime > PLAY_PREFETCH_S) return;
    requestWindow();
  }

  function maybeFinishPlayback() {
    if (state.playState !== 'playing' || !state.playExhausted) return false;
    const ctx = state.playCtx;
    const sched = state.playSched;
    if (!ctx || !sched) return false;
    if (ctx.currentTime < sched.nextTime) return false;
    finishPlayback();
    return true;
  }

  /** 停在 `positionS`：状态回 idle，队列清空，上下文挂起但保留。 */
  function stopAt(positionS) {
    const duration = playDurationS();
    state.playPos = Math.max(0, Math.min(duration, Number(positionS) || 0));
    state.playExhausted = false;
    state.playState = 'idle';
    stopLoops();
    stopSources();
    if (state.playCtx && state.playSched) {
      state.playSched.reset(state.playCtx.currentTime);
      state.playCtx.suspend().catch(() => {});
    }
    // 停下来就不再「正在播这一句」：高亮只跟着播放态走。
    highlightRow(null);
    updatePlayUI();
  }

  /** 正常播完：进度条拉满、状态回 idle。 */
  function finishPlayback() {
    stopAt(playDurationS());
  }

  /** 硬停：位置归零。录音开始、换会话、播放条变灰时调用。 */
  function stopPlayback() {
    state.playToken += 1;
    state.playState = 'idle';
    state.playExhausted = false;
    state.playPos = 0;
    state.playScrub = false;
    state.playResume = false;
    stopLoops();
    stopSources();
    if (state.playCtx && state.playSched) {
      state.playSched.reset(state.playCtx.currentTime);
      state.playCtx.suspend().catch(() => {});
    }
    highlightRow(null);
    updatePlayUI();
  }

  function pausePlayback() {
    if (state.playState !== 'playing') return;
    state.playState = 'paused';
    stopLoops();
    if (state.playCtx) state.playCtx.suspend().catch(() => {});
    updatePlayUI();
    announce('已暂停');
  }

  async function resumePlayback() {
    const ctx = state.playCtx;
    if (!ctx) return;
    state.playState = 'playing';
    try {
      await ctx.resume();
    } catch {
      /* 被系统挂起时留待下一次手势 */
    }
    startLoops();
    updatePlayUI();
    pump();
    announce('继续播放');
  }

  async function startPlayback() {
    const duration = playDurationS();
    if (duration <= 0) return;
    const ctx = ensurePlayCtx();
    if (!ctx) return;

    if (state.playPos >= duration - 0.05) state.playPos = 0; // 播完再按播放＝从头
    const seekId = state.playSeekId;
    state.playToken += 1;
    state.playExhausted = false;
    stopLoops();
    stopSources();
    // 先等上一次 seek/stop 留下的读取落地（结果已被 token 判为过期），
    // 否则 requestWindow() 会把它当成自己的请求，新位置一块也排不上。
    if (state.playLoad) {
      try {
        await state.playLoad;
      } catch {
        /* loadNextWindow 自己吞掉了错误 */
      }
      if (seekId !== state.playSeekId) return; // 等的时候来了 seek，交给它
    }

    state.playState = 'playing';
    state.playSched.reset(ctx.currentTime);
    try {
      await ctx.resume(); // 必须在用户手势里：新上下文默认是 suspended
    } catch {
      /* 同上 */
    }
    startLoops();
    updatePlayUI();
    await requestWindow(); // 首块不等 500 ms 的 pump，点下去就排
    if (state.playState === 'playing') announce('开始播放');
  }

  function togglePlay() {
    if (state.recording || playDurationS() <= 0) return;
    if (state.playState === 'seeking') return; // 正在定位，等它收尾
    if (state.playState === 'playing') {
      pausePlayback();
      return;
    }
    if (state.playState === 'paused') {
      resumePlayback();
      return;
    }
    startPlayback();
  }

  /**
   * 跳到 `seconds` 并（可选）继续播。
   *
   * 步骤固定：作废在途读取 → 停掉全部在播 source → 等最后那笔读取落地 →
   * 重置调度时间线 → 改位置 → 载入该位置窗口 → 回到播放或暂停。
   * seek 到末尾时窗口为空，状态回到 idle 且进度条停在末尾，不会卡在 playing。
   */
  async function seekTo(seconds, options = {}) {
    const duration = playDurationS();
    // 录音中一律不定位：正在写的文件还没定稿，回放声也会被录进去。
    if (duration <= 0 || state.recording) return;
    const ctx = ensurePlayCtx();
    if (!ctx) return;

    const target = Math.min(Math.max(0, Number(seconds) || 0), duration);
    // 连按方向键时会有多次 seek 叠在一起，用 intent 记住「定位完还要不要继续播」，
    // 否则第二次 seek 看到的是 seeking 状态，收尾时会把播放误判成暂停。
    const intent =
      options.play === true ||
      state.playState === 'playing' ||
      (state.playState === 'seeking' && state.playResume);
    state.playResume = intent;
    state.playSeekId += 1;
    const seekId = state.playSeekId;

    state.playToken += 1;
    state.playState = 'seeking';
    state.playExhausted = false;
    stopLoops();
    stopSources();
    if (state.playLoad) {
      try {
        await state.playLoad; // 不能和它并发；它的结果已被 token 判为过期
      } catch {
        /* loadNextWindow 自己吞掉了错误 */
      }
      if (seekId !== state.playSeekId) return; // 等的时候又来了新 seek，交给它
    }

    state.playSched.reset(ctx.currentTime);
    state.playPos = target;
    updatePlayUI();

    await requestWindow();

    if (seekId !== state.playSeekId) return; // 同上：后来的 seek 说了算
    if (state.playState !== 'seeking') return; // 期间又被停止/开始了
    if (intent) {
      state.playState = 'playing';
      try {
        await ctx.resume();
      } catch {
        /* 同上 */
      }
      startLoops();
      pump();
    } else {
      state.playState = 'paused';
      ctx.suspend().catch(() => {});
    }
    updatePlayUI();
  }

  /** 字幕行高亮：定位行一律走 findRow(id)，下一任务换成 Map 时只改这一处。 */
  function highlightRow(id) {
    if (state.playRowId === id) return;
    const previous = state.playRowId ? findRow(state.playRowId) : null;
    if (previous) previous.classList.remove('is-playing');
    state.playRowId = id;
    const row = id ? findRow(id) : null;
    if (row) row.classList.add('is-playing');
  }

  /** 左右方向键是否该接管：回放已开始（含暂停）且不在录音。 */
  function playbackEngaged() {
    return !!Playback && !state.recording && state.playState !== 'idle' && playDurationS() > 0;
  }

  /**
   * 空格归回放的判据：回放已激活（playing / paused / seeking 全算）或最近一次
   * 交互落在播放条内。**暂停态必须算激活**：点字幕起播 → 空格暂停 → 再按空格，
   * 若这里只看 playing，就会掉进 toggleRecording()，一边「暂停」一边开始录音。
   */
  function spaceGoesToPlayback() {
    if (!Playback || state.recording || playDurationS() <= 0) return false;
    if (state.playState !== 'idle') return true;
    return state.playInteracted;
  }

  function wirePlaybar() {
    if (!el.playToggle || !el.playSeek) return;

    el.playToggle.addEventListener('click', () => togglePlay());

    // 拖拽期间只更新数字，松手（change）才真正 seek：每次 input 都读窗口会把
    // 磁盘打满。scrub 只在 input 里置位，避免「点一下滑块不拖动」把标志卡住。
    el.playSeek.addEventListener('input', () => {
      state.playScrub = true;
      renderPlayTime(Number(el.playSeek.value));
    });
    // 拖动松手后一律从新位置开始播（含原来处于暂停/未播的情况）：拖动进度条
    // 本身就是「我要听这一段」，停在暂停态等于没有反馈。
    el.playSeek.addEventListener('change', () => {
      const value = Number(el.playSeek.value);
      state.playScrub = false;
      seekTo(value, { play: true });
    });

    // 「最近一次交互在播放条内」：捕获阶段记，早于任何 click。
    document.addEventListener(
      'pointerdown',
      (event) => {
        state.playInteracted = !!el.playbar && el.playbar.contains(event.target);
      },
      true
    );

    // 点字幕行＝跳到该句开头并开始播。先判能不能播：不能播就只提示、不高亮，
    // 否则行亮了却没跳，是假反馈。
    el.finals.addEventListener('click', (event) => {
      const row = event.target.closest ? event.target.closest('.segment') : null;
      if (!row || !el.finals.contains(row)) return;
      const item = state.finals.find((entry) => entry.id === row.dataset.id);
      if (!item) return;
      if (state.recording || playDurationS() <= 0) {
        const text = state.recording ? '录音结束后才能回放' : '还没有可回放的音频';
        setHint(text);
        announce(text);
        return;
      }
      highlightRow(item.id);
      seekTo(item.start, { play: true });
    });
  }

  /* ─────────────────────────── new session dialog ─────────────────────────── */

  function askNewSession() {
    return new Promise((resolve) => {
      state.dialogResolve = resolve;
      if (!el.newSessionDialog || typeof el.newSessionDialog.showModal !== 'function') {
        resolve(
          window.confirm('开始新的录音会同时清空转写文本与录音音频，且无法恢复。继续吗？')
        );
        state.dialogResolve = null;
        return;
      }
      el.newSessionDialog.showModal();
      el.nsdCancel.focus();
    });
  }

  function finishDialog(value) {
    if (el.newSessionDialog && el.newSessionDialog.open) el.newSessionDialog.close();
    const resolve = state.dialogResolve;
    state.dialogResolve = null;
    if (resolve) resolve(value);
  }

  /* ─────────────────────────── feedback helpers ─────────────────────────── */

  function setHint(text) {
    el.hint.textContent = text;
  }

  function showError(text, options = {}) {
    el.errorText.textContent = text;
    el.errorAlert.hidden = false;
    el.retryBtn.hidden = !options.retry;
    el.troubleshoot.hidden = !options.troubleshoot;
  }

  function clearError() {
    el.errorAlert.hidden = true;
  }

  function showNotice(text, options = {}) {
    el.noticeText.textContent = text;
    el.notice.hidden = false;
    if (options.actionLabel) {
      el.noticeAction.textContent = options.actionLabel;
      el.noticeAction.hidden = false;
      el.noticeAction.onclick = options.action || null;
    } else {
      el.noticeAction.hidden = true;
      el.noticeAction.onclick = null;
    }
  }

  function clearNotice() {
    el.notice.hidden = true;
  }

  function announce(text) {
    if (!el.srStatus) return;
    el.srStatus.textContent = '';
    window.setTimeout(() => {
      el.srStatus.textContent = text;
    }, 40);
  }

  function setConnPill(kind, text) {
    el.connPill.classList.toggle('ok', kind === 'ok');
    el.connPill.classList.toggle('bad', kind === 'bad');
    el.connPill.querySelector('span').textContent = text;
  }

  function setModelPill(kind, text) {
    el.modelPill.classList.toggle('ok', kind === 'ok');
    el.modelPill.classList.toggle('bad', kind === 'bad');
    el.modelPill.querySelector('span').textContent = text;
  }

  async function copyTranscript() {
    const text = state.finals.map((item) => item.text).join('\n');
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const area = document.createElement('textarea');
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand('copy');
      area.remove();
    }
    const original = el.copyBtn.textContent;
    el.copyBtn.textContent = '已复制';
    announce('已复制全部文字');
    setTimeout(() => {
      el.copyBtn.textContent = original;
    }, 1200);
  }

  /* ─────────────────────────── events ─────────────────────────── */

  el.recordBtn.addEventListener('click', () => toggleRecording());
  el.copyBtn.addEventListener('click', copyTranscript);
  el.clearBtn.addEventListener('click', () => clearTranscript(true));
  el.retryBtn.addEventListener('click', retryModel);
  wirePlaybar();

  el.exportBtn.addEventListener('click', (event) => {
    event.stopPropagation();
    if (el.exportMenu.hidden) openMenu();
    else closeMenu(true);
  });

  el.exportBtn.addEventListener('keydown', (event) => {
    if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) {
      event.preventDefault();
      openMenu();
    }
  });

  el.exportMenu.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-format]');
    if (button && !button.disabled) exportAs(button.dataset.format);
  });

  el.exportMenu.addEventListener('keydown', (event) => {
    const items = enabledItems();
    const index = items.indexOf(document.activeElement);

    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        if (items.length) items[(index + 1) % items.length].focus();
        break;
      case 'ArrowUp':
        event.preventDefault();
        if (items.length) items[(index - 1 + items.length) % items.length].focus();
        break;
      case 'Home':
        event.preventDefault();
        if (items.length) items[0].focus();
        break;
      case 'End':
        event.preventDefault();
        if (items.length) items[items.length - 1].focus();
        break;
      case 'Escape':
        event.preventDefault();
        closeMenu(true);
        break;
      case 'Tab':
        closeMenu(false);
        break;
      default:
        break;
    }
  });

  document.addEventListener('click', (event) => {
    if (!el.exportMenu.hidden && !event.target.closest('.export')) closeMenu(false);
  });

  el.language.addEventListener('change', () => {
    state.language = el.language.value;
    sendConfig();
    announce(`识别语言已切换为${Expo.languageLabel(state.language, state.languages)}`);
  });

  if (el.modelSelect) {
    el.modelSelect.addEventListener('change', () => switchModel(el.modelSelect.value));
  }

  if (el.hotwordsInput) {
    el.hotwordsInput.value = state.hotwords;
    let hotwordsTimer = null;
    el.hotwordsInput.addEventListener('input', () => {
      state.hotwords = el.hotwordsInput.value.trim().slice(0, 200);
      saveHotwords(state.hotwords);
      // Typing is not a reason to rebuild the decoder prompt on every key:
      // debounce until the user pauses.
      clearTimeout(hotwordsTimer);
      hotwordsTimer = setTimeout(sendConfig, 400);
    });
  }

  /**
   * 空格：输入焦点在控件里时一律不拦截（原生行为优先）。之后按「最近一次交互
   * 是否在播放条内 / 是否正在出声」决定归回放还是归录音，录音是兜底——这与
   * v0.11 的语义一致，只是多了回放这一层优先级。
   *
   * 左右方向键只在回放激活（含暂停）时接管 ±5 s，控件内的方向键留给控件。
   */
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !el.exportMenu.hidden) {
      closeMenu(true);
      return;
    }

    const target = event.target;
    if (!target) return;
    const tag = (target.tagName || '').toUpperCase();
    const isFormControl =
      target.isContentEditable ||
      ['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON', 'A', 'OPTION'].includes(tag);

    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      if (isFormControl || event.altKey || event.ctrlKey || event.metaKey) return;
      if (!playbackEngaged()) return;
      event.preventDefault(); // 按住不放也不让它滚页面
      // 按住不放不连发：每按一次读一块 30 s 窗口，自动重复会把磁盘刷爆。
      if (event.repeat) return;
      const step = event.key === 'ArrowLeft' ? -PLAY_SEEK_STEP_S : PLAY_SEEK_STEP_S;
      seekTo(playPosition() + step);
      return;
    }

    if (event.code !== 'Space' && event.key !== ' ') return;
    if (isFormControl) return;
    if (!el.exportMenu.hidden) return;
    if (event.repeat) return;

    event.preventDefault();
    if (spaceGoesToPlayback()) {
      togglePlay();
      return;
    }
    toggleRecording();
  });

  if (el.newSessionDialog) {
    el.nsdCancel.addEventListener('click', () => finishDialog(false));
    el.nsdExport.addEventListener('click', () => {
      finishDialog(false);
      openMenu();
    });
    el.nsdConfirm.addEventListener('click', () => finishDialog(true));
    el.newSessionDialog.addEventListener('cancel', (event) => {
      event.preventDefault();
      finishDialog(false);
    });
  }

  window.addEventListener('resize', () => {
    if (!state.recording) drawIdleWaveform();
  });

  // The transcript is memory-only: refreshing loses it. Say so before unload
  // instead of letting the user discover it afterwards (PRD R-10 / PR-06).
  // 音频已经边录边落盘，这里刻意不 finish()：finish 会关句柄，用户点「留下」
  // 之后录音就静默停写了；flush 是无副作用的尽力而为，已写内容尽量刷到磁盘。
  // 真正被强关的会话会留下 .pcm.tmp，下次启动由 reportUnfinishedRecordings() 提示。
  window.addEventListener('beforeunload', (event) => {
    const hasData = state.finals.length > 0 || (state.ledger ? state.ledger.sampleCount > 0 : false);
    if (!hasData) return;
    if (state.recording && state.store) state.store.flush();
    event.preventDefault();
    event.returnValue = '';
  });

  /* ─────────────────────────── boot ─────────────────────────── */

  async function boot() {
    state.reduceMotion =
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    drawIdleWaveform();
    updateExportState();
    reportUnfinishedRecordings();
    await loadConfig();
    connect();
    pollStatus();
  }

  boot();
})();
