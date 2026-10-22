/**
 * EchoNote OPFS 写盘 Worker（经典 Worker，非 ESM module）。
 *
 * createSyncAccessHandle() 只能在 Worker 里调用：主线程调用会抛
 * NotAllowedError。主线程把 Int16 PCM 帧的 ArrayBuffer 发进来，这里按
 * 消息顺序同步写进 OPFS 文件，因此录音的内存占用与时长无关。
 *
 * 每条消息都带 `id`，回复与错误原样带回，主线程据此关联请求——不能靠
 * 回复顺序，因为 append 是即发即忘的。
 *
 * 主线程 → Worker：{id, type:'open', name} / {id, type:'append', buffer}
 *                  / {id, type:'finish'} / {id, type:'read', from, to}
 *                  / {type:'flush'}（无回复，beforeunload 尽力刷盘）
 * Worker → 主线程：{id, type:'opened', offset} / {type:'progress', offset}
 *                  / {id, type:'finished', offset} / {id, type:'data', buffer}
 *                  / {id, type:'error', error}
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
        await open(message);
        break;
      case 'append':
        append(message);
        break;
      case 'finish':
        finish(message);
        break;
      case 'read':
        read(message);
        break;
      case 'flush':
        if (handle) handle.flush();
        break;
      default:
        break;
    }
  } catch (error) {
    self.postMessage({
      id: message.id,
      type: 'error',
      error: (error && error.message) || String(error),
    });
  }
}

async function open(message) {
  const name = message.name;
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
  self.postMessage({ id: message.id, type: 'opened', offset });
}

function append(message) {
  if (!handle) throw new Error('append 之前必须先 open');
  offset += handle.write(new Uint8Array(message.buffer), { at: offset });
  if (offset - reported >= PROGRESS_STEP_BYTES) {
    reported = offset;
    self.postMessage({ type: 'progress', offset });
  }
}

function finish(message) {
  if (handle) {
    handle.flush();
    handle.close();
    handle = null;
  }
  self.postMessage({ id: message.id, type: 'finished', offset });
}

function read(message) {
  if (!handle) throw new Error('文件已关闭，改由主线程通过文件句柄读取');
  const length = Math.max(0, Math.min(message.to, offset) - message.from);
  const buffer = new ArrayBuffer(length);
  handle.read(new Uint8Array(buffer), { at: message.from });
  self.postMessage({ id: message.id, type: 'data', buffer }, [buffer]);
}
