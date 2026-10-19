/* EchoNote 声记 — frontend */
(() => {
  'use strict';

  const SAMPLE_RATE = 16000;

  const el = {
    recordBtn: document.getElementById('record-btn'),
    timer: document.getElementById('timer'),
    hint: document.getElementById('hint'),
    modelTag: document.getElementById('model-tag'),
    footerModel: document.getElementById('footer-model'),
    connPill: document.getElementById('conn-pill'),
    modelPill: document.getElementById('model-pill'),
    language: document.getElementById('language'),
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
  };

  const state = {
    ws: null,
    connected: false,
    modelReady: false,
    recording: false,
    startedAt: 0,
    timerId: null,
    audioCtx: null,
    mediaStream: null,
    sourceNode: null,
    workletNode: null,
    analyser: null,
    audioChunks: [],
    finals: [],
    partial: '',
    language: 'auto',
    rafId: null,
    reconnectTimer: null,
    pollTimer: null,
  };

  /* ─────────────────────────── websocket ─────────────────────────── */

  function connect() {
    if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }

    setConnPill('idle', '连接中');
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}/ws`);
    ws.binaryType = 'arraybuffer';
    state.ws = ws;

    ws.addEventListener('open', () => {
      state.connected = true;
      setConnPill('ok', '已连接');
      sendConfig();
      if (!state.recording) updateRecordEnabled();
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
      state.connected = false;
      setConnPill('bad', '已断开');
      if (state.recording) stopRecording(true);
      updateRecordEnabled();
      clearTimeout(state.reconnectTimer);
      state.reconnectTimer = setTimeout(connect, 1500);
    });

    ws.addEventListener('error', () => {
      /* close handler schedules the reconnect */
    });
  }

  function sendConfig() {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify({ type: 'config', language: state.language }));
    }
  }

  function handleMessage(message) {
    switch (message.type) {
      case 'status':
        updateModelStatus(message);
        break;
      case 'partial':
        state.partial = message.text || '';
        renderPartial();
        break;
      case 'final':
        state.partial = '';
        state.finals.push(message);
        appendFinal(message);
        renderPartial();
        updateExportState();
        if (state.recording) setHint('正在录音 · 实时转写中');
        break;
      case 'done':
        break;
      default:
        break;
    }
  }

  function modelDisplayName(model) {
    if (!model) return 'small';
    const parts = String(model).split(/[\\/]/);
    return parts[parts.length - 1] || model;
  }

  function updateModelStatus(message) {
    const ready = !!message.ready;
    const model = modelDisplayName(message.model);
    state.modelReady = ready;
    el.modelTag.textContent = model;
    el.footerModel.textContent = model;

    if (message.error) {
      setModelPill('bad', '模型加载失败');
      setHint('模型加载失败：' + message.error);
    } else if (ready) {
      setModelPill('ok', '模型就绪');
      if (!state.recording) setHint('点击开始录音');
    } else {
      setModelPill('idle', '模型加载中');
      setHint('正在准备模型…');
    }
    updateRecordEnabled();
  }

  function updateRecordEnabled() {
    const canRecord = state.modelReady && state.connected;
    el.recordBtn.disabled = !canRecord && !state.recording;
    el.language.disabled = !state.modelReady;
  }

  async function pollStatus() {
    try {
      const res = await fetch('/api/status', { cache: 'no-store' });
      if (res.ok) {
        const data = await res.json();
        updateModelStatus({ type: 'status', ...data });
      }
    } catch {
      /* server not up yet */
    }
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(pollStatus, state.modelReady ? 15000 : 2000);
  }

  async function loadConfig() {
    try {
      const res = await fetch('/api/config', { cache: 'no-store' });
      if (!res.ok) return;
      const config = await res.json();
      const model = modelDisplayName(config.model);
      el.modelTag.textContent = model;
      el.footerModel.textContent = model;

      el.language.innerHTML = '';
      (config.languages || []).forEach((lang) => {
        const option = document.createElement('option');
        option.value = lang.code;
        option.textContent = lang.label;
        el.language.appendChild(option);
      });

      const defaultLanguage = config.language || 'zh';
      el.language.value = defaultLanguage;
      state.language = el.language.value;
      sendConfig();
    } catch {
      /* defaults stay */
    }
  }

  /* ─────────────────────────── recording ─────────────────────────── */

  async function toggleRecording() {
    if (state.recording) {
      stopRecording();
    } else {
      await startRecording();
    }
  }

  async function startRecording() {
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
    if (state.finals.length > 0 && !confirm('开始新的录音会清空当前转写内容，继续吗？')) {
      return;
    }

    try {
      state.mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
    } catch (error) {
      setHint('无法访问麦克风：' + friendlyMediaError(error));
      return;
    }

    try {
      const ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
      state.audioCtx = ctx;
      if (ctx.state === 'suspended') await ctx.resume();
      await ctx.audioWorklet.addModule('pcm-worklet.js');

      const source = ctx.createMediaStreamSource(state.mediaStream);
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

      worklet.port.onmessage = (event) => {
        const chunk = new Int16Array(event.data);
        state.audioChunks.push(chunk);
        if (state.ws && state.ws.readyState === WebSocket.OPEN) {
          state.ws.send(event.data);
        }
      };

      state.sourceNode = source;
      state.workletNode = worklet;
      state.analyser = analyser;

      resetSession();
      state.recording = true;
      state.startedAt = performance.now();
      el.recordBtn.classList.add('recording');
      el.recordBtn.setAttribute('aria-label', '停止录音');
      el.recordBtn.disabled = false;
      setHint('正在录音 · 实时转写中');
      startTimer();
      drawWaveform();
    } catch (error) {
      console.error(error);
      setHint('音频初始化失败：' + (error.message || error.name));
      cleanupAudio();
    }
  }

  function stopRecording(silent = false) {
    if (!state.recording) return;

    state.recording = false;
    el.recordBtn.classList.remove('recording');
    el.recordBtn.setAttribute('aria-label', '开始录音');
    stopTimer();
    cancelAnimationFrame(state.rafId);
    state.rafId = null;
    cleanupAudio();

    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify({ type: 'flush' }));
    }

    if (!silent) {
      setHint('已停止 · 正在生成最后一段…');
    }
    drawIdleWaveform();
    updateRecordEnabled();
    updateExportState();
  }

  function resetSession() {
    state.audioChunks = [];
    state.finals = [];
    state.partial = '';
    el.finals.innerHTML = '';
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

  /* ─────────────────────────── timer ─────────────────────────── */

  function startTimer() {
    stopTimer();
    const tick = () => {
      const seconds = Math.floor((performance.now() - state.startedAt) / 1000);
      el.timer.textContent = formatClock(seconds);
    };
    tick();
    state.timerId = setInterval(tick, 200);
  }

  function stopTimer() {
    if (state.timerId) {
      clearInterval(state.timerId);
      state.timerId = null;
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

    // Subtle idle baseline with a soft glow.
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
      const value = data[i] / 128 - 1; // -1 .. 1
      const x = i * step;
      const y = height / 2 + value * (height * 0.34);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();

    state.rafId = requestAnimationFrame(drawWaveform);
  }

  /* ─────────────────────────── transcript ─────────────────────────── */

  function appendFinal(message) {
    el.emptyState.hidden = true;
    const item = document.createElement('div');
    item.className = 'segment';

    const ts = document.createElement('span');
    ts.className = 'ts';
    ts.textContent = formatClock(Math.floor(message.start || 0));

    const text = document.createElement('p');
    text.textContent = message.text;

    item.appendChild(ts);
    item.appendChild(text);
    el.finals.appendChild(item);
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
    if (nearBottom) {
      box.scrollTop = box.scrollHeight;
    }
  }

  function clearTranscript(ask = true) {
    if (ask && state.finals.length > 0 && !confirm('清空当前转写内容？录音文件不会删除。')) {
      return;
    }
    state.finals = [];
    state.partial = '';
    el.finals.innerHTML = '';
    el.emptyState.hidden = false;
    renderPartial();
    updateExportState();
  }

  function updateExportState() {
    const hasText = state.finals.length > 0;
    el.copyBtn.disabled = !hasText;
    el.clearBtn.disabled = !hasText && !state.partial;
    const wavButton = el.exportMenu.querySelector('[data-format="wav"]');
    const textButtons = el.exportMenu.querySelectorAll('[data-format="txt"], [data-format="md"], [data-format="srt"]');
    if (wavButton) wavButton.disabled = state.audioChunks.length === 0;
    textButtons.forEach((button) => {
      button.disabled = !hasText;
    });
  }

  /* ─────────────────────────── export ─────────────────────────── */

  function closeMenu() {
    el.exportMenu.hidden = true;
  }

  function toggleMenu() {
    el.exportMenu.hidden = !el.exportMenu.hidden;
  }

  function exportAs(format) {
    closeMenu();

    if (format === 'wav') {
      if (state.audioChunks.length === 0) return;
      download(buildWav(), fileName('wav'), 'audio/wav');
      return;
    }
    if (state.finals.length === 0) return;

    if (format === 'txt') {
      download(new Blob([buildTxt()], { type: 'text/plain;charset=utf-8' }), fileName('txt'));
    } else if (format === 'md') {
      download(new Blob([buildMarkdown()], { type: 'text/markdown;charset=utf-8' }), fileName('md'));
    } else if (format === 'srt') {
      download(new Blob([buildSrt()], { type: 'application/x-subrip;charset=utf-8' }), fileName('srt'));
    }
  }

  function headerLines() {
    const started = new Date();
    return {
      date: started.toLocaleString('zh-CN', { hour12: false }),
      duration: el.timer.textContent,
    };
  }

  function buildTxt() {
    const meta = headerLines();
    const lines = [
      'EchoNote 转写记录',
      `时间：${meta.date}`,
      `时长：${meta.duration}`,
      '',
      ...state.finals.map((item) => `[${formatClock(Math.floor(item.start || 0))}] ${item.text}`),
      '',
    ];
    return lines.join('\n');
  }

  function buildMarkdown() {
    const meta = headerLines();
    const lines = [
      '# EchoNote 转写记录',
      '',
      `- **时间**：${meta.date}`,
      `- **时长**：${meta.duration}`,
      `- **模型**：${el.modelTag.textContent}`,
      '',
      '---',
      '',
      ...state.finals.map((item) => `- \`${formatClock(Math.floor(item.start || 0))}\` ${item.text}`),
      '',
    ];
    return lines.join('\n');
  }

  function buildSrt() {
    return state.finals
      .map((item, index) => {
        const start = Number(item.start || 0);
        const end = Math.max(Number(item.end || 0), start + 1.5);
        return `${index + 1}\n${srtTime(start)} --> ${srtTime(end)}\n${item.text}\n`;
      })
      .join('\n');
  }

  function buildWav() {
    const total = state.audioChunks.reduce((sum, chunk) => sum + chunk.length, 0);
    const buffer = new ArrayBuffer(44 + total * 2);
    const view = new DataView(buffer);

    const writeString = (offset, text) => {
      for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i));
    };

    writeString(0, 'RIFF');
    view.setUint32(4, 36 + total * 2, true);
    writeString(8, 'WAVE');
    writeString(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true); // PCM
    view.setUint16(22, 1, true); // mono
    view.setUint32(24, SAMPLE_RATE, true);
    view.setUint32(28, SAMPLE_RATE * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeString(36, 'data');
    view.setUint32(40, total * 2, true);

    let offset = 44;
    state.audioChunks.forEach((chunk) => {
      new Int16Array(buffer, offset, chunk.length).set(chunk);
      offset += chunk.length * 2;
    });

    return new Blob([buffer], { type: 'audio/wav' });
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

  function fileName(extension) {
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}`;
    return `echonote-${stamp}.${extension}`;
  }

  /* ─────────────────────────── helpers ─────────────────────────── */

  function formatClock(totalSeconds) {
    const seconds = Math.max(0, Math.floor(totalSeconds || 0));
    const mm = String(Math.floor(seconds / 60)).padStart(2, '0');
    const ss = String(seconds % 60).padStart(2, '0');
    return `${mm}:${ss}`;
  }

  function srtTime(totalSeconds) {
    const ms = Math.max(0, Math.round((totalSeconds || 0) * 1000));
    const hours = Math.floor(ms / 3600000);
    const minutes = Math.floor((ms % 3600000) / 60000);
    const seconds = Math.floor((ms % 60000) / 1000);
    const millis = ms % 1000;
    const pad = (n, size = 2) => String(n).padStart(size, '0');
    return `${pad(hours)}:${pad(minutes)}:${pad(seconds)},${pad(millis, 3)}`;
  }

  function setHint(text) {
    el.hint.textContent = text;
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
    setTimeout(() => {
      el.copyBtn.textContent = original;
    }, 1200);
  }

  /* ─────────────────────────── events ─────────────────────────── */

  el.recordBtn.addEventListener('click', toggleRecording);
  el.copyBtn.addEventListener('click', copyTranscript);
  el.clearBtn.addEventListener('click', () => clearTranscript(true));
  el.exportBtn.addEventListener('click', (event) => {
    event.stopPropagation();
    toggleMenu();
  });
  el.exportMenu.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-format]');
    if (button && !button.disabled) exportAs(button.dataset.format);
  });
  document.addEventListener('click', (event) => {
    if (!el.exportMenu.hidden && !event.target.closest('.export')) closeMenu();
  });

  el.language.addEventListener('change', () => {
    state.language = el.language.value;
    sendConfig();
  });

  document.addEventListener('keydown', (event) => {
    const tag = (event.target && event.target.tagName) || '';
    if (['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON'].includes(tag)) return;
    if (event.code === 'Space') {
      event.preventDefault();
      toggleRecording();
    }
  });

  window.addEventListener('resize', () => {
    if (!state.recording) drawIdleWaveform();
  });

  window.addEventListener('beforeunload', () => {
    if (state.recording) stopRecording(true);
  });

  /* ─────────────────────────── boot ─────────────────────────── */

  loadConfig();
  connect();
  pollStatus();
  drawIdleWaveform();
  updateExportState();
})();
