/**
 * EchoNote OPFS 写盘 Worker（经典 Worker，非 ESM module）。
 *
 * createSyncAccessHandle() 只能在 Worker 里调用：主线程调用会抛
 * NotAllowedError。主线程把 Int16 PCM 帧的 ArrayBuffer 发进来，这里按
 * 消息顺序同步写进 OPFS 文件，因此录音的内存占用与时长无关。
 *
 * 主线程 → Worker：{type:'open', name} / {type:'append', buffer}
 *                  / {type:'finish'} / {type:'read', from, to}
 * Worker → 主线程：{type:'opened', offset} / {type:'progress', offset}
 *                  / {type:'finished', offset} / {type:'data', buffer}
 *                  / {type:'error', error}
 */
'use strict';

const PROGRESS_STEP_BYTES = 32000;

let handle = null;
let offset = 0;
let reported = 0;

self.addEventListener('message', (event) => {
  run(event.data || {});
});

async function run(message) {
  try {
    switch (message.type) {
      case 'open':
        await open(message.name);
        break;
      case 'append':
        append(message.buffer);
        break;
      case 'finish':
        finish();
        break;
      case 'read':
        read(message.from, message.to);
        break;
      default:
        break;
    }
  } catch (error) {
    self.postMessage({ type: 'error', error: (error && error.message) || String(error) });
  }
}

async function open(name) {
  const root = await navigator.storage.getDirectory();
  const tmpName = `${name}.pcm.tmp`;
  let fileHandle = await root.getFileHandle(tmpName, { create: true });
  let size = (await fileHandle.getFile()).size;

  if (size === 0) {
    // 连接中断后继续录音：上一段已经改名成 .pcm，接回来接着往后写。
    const doneHandle = await root.getFileHandle(`${name}.pcm`).catch(() => null);
    const doneSize = doneHandle ? (await doneHandle.getFile()).size : 0;
    if (doneSize > 0 && typeof doneHandle.move === 'function') {
      await root.removeEntry(tmpName);
      await doneHandle.move(tmpName);
      fileHandle = await root.getFileHandle(tmpName);
      size = doneSize;
    }
  }

  handle = await fileHandle.createSyncAccessHandle();
  offset = size;
  reported = size;
  self.postMessage({ type: 'opened', offset });
}

function append(buffer) {
  if (!handle) throw new Error('append 之前必须先 open');
  offset += handle.write(new Uint8Array(buffer), { at: offset });
  if (offset - reported >= PROGRESS_STEP_BYTES) {
    reported = offset;
    self.postMessage({ type: 'progress', offset });
  }
}

function finish() {
  if (handle) {
    handle.flush();
    handle.close();
    handle = null;
  }
  self.postMessage({ type: 'finished', offset });
}

function read(from, to) {
  if (!handle) throw new Error('文件已关闭，改由主线程通过文件句柄读取');
  const length = Math.max(0, Math.min(to, offset) - from);
  const buffer = new ArrayBuffer(length);
  handle.read(new Uint8Array(buffer), { at: from });
  self.postMessage({ type: 'data', buffer }, [buffer]);
}
