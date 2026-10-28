/**
 * 「离线翻译模型」设置块 —— 只在原生壳里出现。
 *
 * 背景：翻译模型每方向 148 MB（解压 237 MB），不随 APK 发布。以前唯一的
 * 安装方式是把 zip 用 adb / 文件管理器塞进 `Android/data/.../files/models/`，
 * 签名包的用户实际上装不上。这里补上应用内下载：点一下，原生侧拉包、校验
 * sha256、解压、落位，装完「译文语言」下拉立刻出现。
 *
 * 本模块由 native-boot.js 动态引入，桌面/浏览器环境永远不会执行；即便执行，
 * `mt.supported()` 为假时整块保持隐藏，不留一个点了没反应的按钮。
 */

import { mt } from './native-translate.js?v=5038fb639a';

const DIRECTIONS = {
  'zh-en': '中文 → 英文',
  'en-zh': '英文 → 中文',
};

/** 154 万 → 「154 MB」；只用于界面，不做精度承诺。 */
export function formatMb(bytes) {
  const value = Number(bytes) || 0;
  if (value <= 0) return '0 MB';
  const mb = value / (1024 * 1024);
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
}

/**
 * 进度事件 → 一行状态文案。纯函数：node 里能直接测，不必起 WebView。
 * phase ∈ resolve / download / verify / unzip / done。
 */
export function progressLabel(ev) {
  const phase = ev && ev.phase;
  if (phase === 'resolve') return '准备中…';
  if (phase === 'download') {
    const received = Number(ev.received) || 0;
    const total = Number(ev.total) || 0;
    if (total > 0) {
      const pct = Math.min(100, Math.round((received / total) * 100));
      return `下载 ${pct}% · ${formatMb(received)} / ${formatMb(total)}`;
    }
    return `下载 ${formatMb(received)}`;
  }
  if (phase === 'verify') return '校验中…';
  if (phase === 'copy') {
    const received = Number(ev.received) || 0;
    const total = Number(ev.total) || 0;
    if (total > 0) {
      const pct = Math.min(100, Math.round((received / total) * 100));
      return `导入 ${pct}% · ${formatMb(received)} / ${formatMb(total)}`;
    }
    return `导入 ${formatMb(received)}`;
  }
  if (phase === 'unzip') {
    const entries = Number(ev.entries) || 0;
    return entries > 0 ? `解压中 ${entries} 个文件` : '解压中…';
  }
  return '';
}

/** 进度条比例；总量未知返回 null（此时不画条，只留文字）。 */
export function progressRatio(ev) {
  if (!ev || ev.phase !== 'download') return null;
  const total = Number(ev.total) || 0;
  if (total <= 0) return null;
  const received = Number(ev.received) || 0;
  return Math.min(1, Math.max(0, received / total));
}

function renderIdle(entry, installed, bytes) {
  entry.busy = false;
  entry.bar.hidden = true;
  entry.fill.style.width = '0%';
  entry.state.dataset.state = installed ? 'installed' : 'idle';
  entry.state.textContent = installed ? `已安装 · 占用 ${formatMb(bytes)}` : '未安装';
  entry.state.removeAttribute('title');
  entry.action.disabled = installed;
  entry.action.textContent = installed ? '已安装' : '下载';
  entry.action.dataset.intent = installed ? '' : 'download';
  // 装好了就不必再提"从文件导入"，免得每一行都挂三条文字。
  entry.import.hidden = installed;
  entry.import.disabled = false;
}

function renderBusy(entry, text) {
  entry.busy = true;
  entry.state.dataset.state = 'busy';
  entry.state.textContent = text;
  entry.action.disabled = false;
  entry.action.textContent = '取消';
  entry.action.dataset.intent = 'cancel';
  entry.import.hidden = true;
}

function renderFailed(entry, message) {
  entry.busy = false;
  entry.pending = false;
  entry.bar.hidden = true;
  entry.state.dataset.state = 'failed';
  entry.state.textContent = `失败：${message}`;
  entry.state.title = message;
  entry.action.disabled = false;
  entry.action.textContent = '重试';
  entry.action.dataset.intent = 'download';
  // 网络这条路失败时，本地导入往往就是解药——把入口留着。
  entry.import.hidden = false;
  entry.import.disabled = false;
}

export async function initMtModels() {
  const panel = document.getElementById('mt-models');
  if (!panel || panel.dataset.ready === '1') return;
  if (!mt.supported()) return;
  panel.dataset.ready = '1';
  panel.hidden = false;

  const meta = panel.querySelector('[data-role="meta"]');
  const rows = new Map();

  panel.querySelectorAll('.mt-row').forEach((row) => {
    const direction = row.dataset.direction;
    if (!DIRECTIONS[direction]) return;
    const entry = {
      row,
      state: row.querySelector('[data-role="state"]'),
      action: row.querySelector('[data-role="action"]'),
      import: row.querySelector('[data-role="import"]'),
      bar: row.querySelector('[data-role="bar"]'),
      fill: row.querySelector('[data-role="bar"] i'),
      busy: false,
      pending: false,
    };
    entry.action.addEventListener('click', () => onAction(direction));
    entry.import.addEventListener('click', () => onImport(direction));
    rows.set(direction, entry);
  });

  mt.onProgress((ev) => {
    const entry = rows.get(ev.direction);
    if (!entry || !entry.busy) return;
    const label = progressLabel(ev);
    if (label) entry.state.textContent = label;
    const ratio = progressRatio(ev);
    entry.bar.hidden = ratio === null;
    if (ratio !== null) entry.fill.style.width = `${Math.round(ratio * 100)}%`;
  });

  async function refresh() {
    const status = await mt.status();
    if (!status.supported) return;
    if (meta) {
      meta.textContent =
        status.freeBytes > 0
          ? `每方向约 154 MB · 可用 ${formatMb(status.freeBytes)}`
          : '每方向约 154 MB，建议 Wi-Fi 下载';
    }
    for (const [direction, entry] of rows) {
      const item = status.items.find((it) => it && it.direction === direction);
      renderIdle(entry, !!(item && item.installed), item && item.bytes);
      // 上一次安装还在跑（比如界面重载过），把真实状态盖回去。
      if (status.installing === direction) renderBusy(entry, '安装中…');
    }
  }

  /** 装完把「译文语言」下拉叫出来：目标语种从已安装方向反推。 */
  async function syncTranslate() {
    const status = await mt.status();
    const targets = status.items
      .filter((it) => it && it.installed && it.target)
      .map((it) => it.target);
    const app = window.EchoNote;
    if (app && typeof app.refreshTranslateTargets === 'function') {
      app.refreshTranslateTargets(targets);
    }
    // native-boot 缓存着探测结果，config 帧还会再用一次；通知它一起更新。
    window.dispatchEvent(new CustomEvent('echonote:mt-installed', { detail: { targets } }));
  }

  async function onAction(direction) {
    const entry = rows.get(direction);
    if (!entry || entry.pending) return;
    const intent = entry.action.dataset.intent;

    if (intent === 'cancel') {
      entry.action.disabled = true;
      entry.action.textContent = '取消中…';
      await mt.cancel();
      return;
    }
    if (intent !== 'download') return;

    entry.pending = true;
    renderBusy(entry, '准备中…');
    entry.bar.hidden = true;
    try {
      const res = await mt.download(direction);
      entry.pending = false;
      if (res && res.cancelled) {
        await refresh();
        entry.state.dataset.state = 'idle';
        entry.state.textContent = '已取消';
        return;
      }
      await refresh();
      await syncTranslate();
    } catch (error) {
      const message = String((error && error.message) || error || '下载失败').replace(
        /^翻译模型安装失败：/,
        '',
      );
      renderFailed(entry, message);
    }
  }

  /** 本地导入：选文件 → 原生复制/解压，成功后同样要把下拉叫出来。 */
  async function onImport(direction) {
    const entry = rows.get(direction);
    if (!entry || entry.pending) return;
    entry.pending = true;
    renderBusy(entry, '请选择模型包…');
    entry.bar.hidden = true;
    try {
      const res = await mt.importZip(direction);
      entry.pending = false;
      if (res && res.cancelled) {
        await refresh();
        return;
      }
      await refresh();
      await syncTranslate();
    } catch (error) {
      entry.pending = false;
      const message = String((error && error.message) || error || '导入失败').replace(
        /^翻译模型(安装|导入)失败：/,
        '',
      );
      renderFailed(entry, message);
    }
  }

  await refresh();
}
