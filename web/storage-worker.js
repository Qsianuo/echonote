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
 *                  / {id, type:'flush'}（尽力刷盘，失败不上报 error）
 * Worker → 主线程：{id, type:'opened', offset} / {type:'progress', offset}
 *                  / {id, type:'finished', offset} / {id, type:'data', buffer}
 *                  / {id, type:'flushed', ok, error?}
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
        flushSafely(message);
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
  const finalName = `${name}.pcm`;
  let fileHandle = await root.getFileHandle(tmpName, { create: true });
  let size = (await fileHandle.getFile()).size;

  if (size === 0) {
    // 连接中断后继续录音：上一段已经写成 .pcm，接回来接着往后写。
    const doneHandle = await root.getFileHandle(finalName).catch(() => null);
    const doneSize = doneHandle ? (await doneHandle.getFile()).size : 0;
    if (doneSize > 0) {
      await adoptFinishedFile(root, doneHandle, tmpName, finalName);
      fileHandle = await root.getFileHandle(tmpName);
      size = doneSize;
    }
  }

  handle = await fileHandle.createSyncAccessHandle();
  offset = size;
  reported = size;
  self.postMessage({ id: message.id, type: 'opened', offset });
}

/**
 * 把已完成的 .pcm 挪回 .pcm.tmp，好接着往后追加。
 *
 * Chrome 有 `FileSystemFileHandle.move()`，一步改名；Firefox / Safari 有 OPFS
 * 但没有 move()，只能"新建目标文件 → 流式拷贝 → 删旧文件"。这里刻意用
 * `file.stream()` 而不是 `arrayBuffer()`：一小时的 16 kHz 单声道 PCM 约 115 MB，
 * 一次性读入会把标签页内存打满，正是落盘要避免的事。
 *
 * 两条路都不可用时抛出，主线程会捕获并把本次录音降级到内存存储。
 */
async function adoptFinishedFile(root, sourceHandle, tmpName, finalName) {
  if (typeof sourceHandle.move === 'function') {
    await root.removeEntry(tmpName);
    await sourceHandle.move(tmpName);
    return;
  }
  const target = await root.getFileHandle(tmpName, { create: true });
  const writable = await target.createWritable(); // 没有就抛 TypeError，由主线程降级
  // pipeTo 按块搬运，峰值内存只有一个流块；结束时 writable 自动 close。
  await (await sourceHandle.getFile()).stream().pipeTo(writable);
  await root.removeEntry(finalName);
}

function append(message) {
  if (!handle) throw new Error('append 之前必须先 open');
  offset += handle.write(new Uint8Array(message.buffer), { at: offset });
  if (offset - reported >= PROGRESS_STEP_BYTES) {
    reported = offset;
    self.postMessage({ type: 'progress', offset });
  }
}

/**
 * flush 是 beforeunload 的尽力而为兜底：出错只回报 `flushed.ok = false`，
 * 绝不走 run() 的 error 通道——主线程把"无主 error"当成致命写盘失败，
 * 一次 flush 失败就会误判整段录音报废。
 */
function flushSafely(message) {
  if (!handle) {
    self.postMessage({ id: message.id, type: 'flushed', ok: false, error: '文件未打开' });
    return;
  }
  try {
    handle.flush();
    self.postMessage({ id: message.id, type: 'flushed', ok: true });
  } catch (error) {
    self.postMessage({
      id: message.id,
      type: 'flushed',
      ok: false,
      error: (error && error.message) || String(error),
    });
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
