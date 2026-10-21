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

  const Expo = window.EchoNoteExport;
  const Wav = window.EchoNoteWav;

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
    audioChunks: [],
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
  };

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
        : '录音与识别均在本机完成，不上传云端，也不写入磁盘';
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

    if (!state.connected) {
      setHint('正在连接服务器…');
      connect();
      return;
    }
    if (!state.modelReady) {
      setHint('模型还在加载，请稍候…');
      return;
    }

    const hasData = state.finals.length > 0 || state.audioChunks.length > 0;
    if (!continueSession && hasData) {
      const confirmed = await askNewSession();
      if (!confirmed) return;
      resetSession();
    }

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
      return;
    }

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
      clearError();
      setHint('正在录音 · 实时转写中');
      startTimer();
      drawWaveform();
      updateRecordEnabled();
      announce('开始录音');
    } catch (error) {
      console.error(error);
      showError(`音频初始化失败：${error.message || error.name}`);
      setHint('音频初始化失败，请刷新页面重试');
      cleanupAudio();
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

  function resetSession() {
    state.audioChunks = [];
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
    state.audioChunks.push(frame);
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
    const bytes = Wav.byteLength(state.audioChunks);
    if (!state.memoryWarned && bytes > MEMORY_BUDGET_BYTES) {
      state.memoryWarned = true;
      const mib = Math.round(bytes / 1048576);
      showNotice(`本次录音已占用约 ${mib} MiB 内存，建议先导出，再开始新录音。`);
    }
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
    const hasAudio = state.audioChunks.length > 0;

    el.copyBtn.disabled = !hasText;
    el.clearBtn.disabled = !hasText && !state.partial;

    el.exportMenu.querySelectorAll('[role="menuitem"]').forEach((button) => {
      const format = button.dataset.format;
      button.disabled = format === 'wav' ? !hasAudio : !hasText;
    });
  }

  function exportAs(format) {
    closeMenu(false);
    const meta = buildMeta();

    if (format === 'wav') {
      if (state.audioChunks.length === 0) return;
      const peakMib = Math.ceil((Wav.byteLength(state.audioChunks) * 2) / 1048576);
      setHint(`正在导出 WAV，预计占用约 ${peakMib} MiB 内存…`);
      const blob = Wav.buildWavBlob(state.audioChunks, SAMPLE_RATE);
      download(blob, Expo.fileName(meta, 'wav'), 'audio/wav');
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
   * Space toggles recording only when it is not being used as an input key.
   * Buttons/links/inputs keep their native behaviour, `event.repeat` is
   * ignored and nothing is preventDefault-ed inside a text field (FR-002).
   */
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !el.exportMenu.hidden) {
      closeMenu(true);
      return;
    }
    if (event.code !== 'Space' && event.key !== ' ') return;

    const target = event.target;
    if (!target) return;
    const tag = (target.tagName || '').toUpperCase();
    const isFormControl =
      target.isContentEditable ||
      ['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON', 'A', 'OPTION'].includes(tag);
    if (isFormControl) return;
    if (!el.exportMenu.hidden) return;
    if (event.repeat) return;

    event.preventDefault();
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
  window.addEventListener('beforeunload', (event) => {
    const hasData = state.finals.length > 0 || state.audioChunks.length > 0;
    if (!hasData) return;
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
    await loadConfig();
    connect();
    pollStatus();
  }

  boot();
})();
