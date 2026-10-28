/**
 * 翻译模型下载桥（web/native-translate.js）的契约。
 *
 * 这一层没有 UI：它只保证"插件在 → 透传，插件不在 → 安静降级"。真正
 * 容易出错的地方（百分比文案、按钮状态）在 mt-models.test.mjs。
 */

import assert from 'node:assert/strict';
import test from 'node:test';

function installPlugin(plugin) {
  globalThis.window = { Capacitor: { Plugins: plugin ? { SherpaMT: plugin } : {} } };
}

const { mt } = await import('../../web/native-translate.js');

test('插件缺席时探测返回空表，而不是抛错', async () => {
  installPlugin(null);
  assert.deepEqual(await mt.probe(), { targets: [] });
  assert.equal(mt.supported(), false);
  assert.equal(await mt.translate('你好', 'en'), '');
  assert.deepEqual(await mt.status(), {
    items: [],
    freeBytes: 0,
    installing: '',
    supported: false,
  });
});

test('probe 只保留非空字符串语种', async () => {
  installPlugin({ available: async () => ({ targets: ['en', '', null, 'zh', 42] }) });
  assert.deepEqual(await mt.probe(), { targets: ['en', 'zh'] });
});

test('status 透传安装状态与可用空间', async () => {
  installPlugin({
    modelStatus: async () => ({
      items: [{ direction: 'zh-en', target: 'en', installed: true, bytes: 236_800_000 }],
      freeBytes: 12_000_000_000,
      installing: '',
    }),
  });
  assert.equal(mt.supported(), true);
  const status = await mt.status();
  assert.equal(status.supported, true);
  assert.equal(status.items.length, 1);
  assert.equal(status.freeBytes, 12_000_000_000);
});

test('status 遇到原生异常时降级为空表，不让设置页崩掉', async () => {
  installPlugin({ modelStatus: async () => { throw new Error('boom'); } });
  const status = await mt.status();
  assert.deepEqual(status.items, []);
  assert.equal(status.supported, false);
});

test('download 把方向原样交给原生；失败信息向上抛', async () => {
  let seen = null;
  installPlugin({ downloadModel: async (args) => { seen = args; return { ok: true, bytes: 1 }; } });
  const res = await mt.download('en-zh');
  assert.deepEqual(seen, { direction: 'en-zh' });
  assert.equal(res.ok, true);

  installPlugin({ downloadModel: async () => { throw new Error('HTTP 404'); } });
  await assert.rejects(() => mt.download('zh-en'), /HTTP 404/);
});

test('插件缺席时 download 明确报错、cancel 是空操作', async () => {
  installPlugin(null);
  await assert.rejects(() => mt.download('zh-en'), /不支持/);
  await mt.cancel(); // 不应抛错
});

test('onProgress 只转发 mtProgress，退订后卸掉监听', async () => {
  let listener = null;
  let removed = 0;
  installPlugin({
    addListener: (name, fn) => {
      assert.equal(name, 'mtProgress');
      listener = fn;
      return { remove: () => { removed += 1; } };
    },
  });

  const seen = [];
  const off = mt.onProgress((data) => seen.push(data));
  listener({ direction: 'zh-en', phase: 'download', received: 1, total: 2 });
  assert.equal(seen.length, 1);
  assert.equal(seen[0].phase, 'download');

  off();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(removed, 1);
});

test('进度回调里抛异常不会打断原生事件流', async () => {
  let listener = null;
  installPlugin({
    addListener: (name, fn) => {
      listener = fn;
      return { remove: () => {} };
    },
  });
  mt.onProgress(() => {
    throw new Error('回调写错了');
  });
  assert.doesNotThrow(() => listener({ direction: 'zh-en', phase: 'unzip', entries: 2 }));
});

test('importZip 把方向交给原生；插件缺席时明确报错', async () => {
  let seen = null;
  installPlugin({ importModel: async (args) => { seen = args; return { ok: true, bytes: 1 }; } });
  const res = await mt.importZip('zh-en');
  assert.deepEqual(seen, { direction: 'zh-en' });
  assert.equal(res.ok, true);

  installPlugin(null);
  await assert.rejects(() => mt.importZip('zh-en'), /不支持/);
});
