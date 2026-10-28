/**
 * 离线翻译桥（原生壳）。
 *
 * 桌面端的翻译跑在 Python 里（ctranslate2 + opus-mt），手机上没有 Python，
 * 只能落在原生侧。本文件只做**桥**：把 Capacitor 插件 `SherpaMT` 的三件事
 * （探测可用语向、翻译一句、报告模型是否下载好）翻译成引擎能用的形状；
 * 插件缺席时静默降级，界面上「译文语言」下拉会自己消失（app.js 只在
 * translate_targets 非空时才显示它）。
 *
 * 降级仍是常态而非异常：模型（每方向约 226 MB）不随 APK 发布，用户没下载就
 * 探测不到，probe() 返回空表——界面上下拉不出现，而不是给一个点了没反应的
 * 选项。译文通道（config 帧的 translate_to、final/refine 的 translation 字段、
 * 导出）与桌面端完全一致。
 */

const PLUGIN = 'SherpaMT';

/** 原生插件实例；不存在（桌面浏览器 / 插件未注册）时返回 null。 */
function bridge() {
  const cap = typeof window !== 'undefined' ? window.Capacitor : null;
  const plugins = cap && cap.Plugins;
  return (plugins && plugins[PLUGIN]) || null;
}

export const mt = {
  /**
   * 探测可用语向。返回 ``{ targets: string[] }``——空数组表示"本机没有翻译
   * 引擎"，UI 据此隐藏下拉，绝不留一个永远翻不出东西的选项。
   */
  async probe() {
    const plugin = bridge();
    if (!plugin || typeof plugin.available !== 'function') return { targets: [] };
    try {
      const res = await plugin.available();
      const targets = Array.isArray(res && res.targets) ? res.targets : [];
      return { targets: targets.filter((code) => typeof code === 'string' && code) };
    } catch (error) {
      console.warn('[native-translate] 探测失败:', error);
      return { targets: [] };
    }
  },

  /** 翻一句。失败或引擎缺席返回空串（= 这段没有译文，原文照常显示）。 */
  async translate(text, target) {
    const plugin = bridge();
    if (!plugin || typeof plugin.translate !== 'function') return '';
    if (!text || !target) return '';
    try {
      const res = await plugin.translate({ text, target });
      return String((res && res.text) || '').trim();
    } catch (error) {
      console.warn('[native-translate] 翻译失败:', error);
      return '';
    }
  },

  /** 插件在不在（决定设置页要不要显示「离线翻译模型」这一块）。 */
  supported() {
    const plugin = bridge();
    return !!plugin && typeof plugin.modelStatus === 'function';
  },

  /**
   * 两个方向的安装状态：``{ items: [{direction, target, installed, bytes, path}],
   * freeBytes, installing }``。插件缺席时 ``supported:false``，界面整块隐藏。
   */
  async status() {
    const plugin = bridge();
    if (!plugin || typeof plugin.modelStatus !== 'function') {
      return { items: [], freeBytes: 0, installing: '', supported: false };
    }
    try {
      const res = await plugin.modelStatus();
      const items = Array.isArray(res && res.items) ? res.items : [];
      return {
        items,
        freeBytes: Number((res && res.freeBytes) || 0),
        installing: String((res && res.installing) || ''),
        supported: true,
      };
    } catch (error) {
      console.warn('[native-translate] 读取模型状态失败:', error);
      return { items: [], freeBytes: 0, installing: '', supported: false };
    }
  },

  /**
   * 下载并安装一个方向（"zh-en" / "en-zh"）。返回安装结果；用户取消时
   * ``{ ok:false, cancelled:true }``，失败时抛出带原因的异常。
   */
  async download(direction) {
    const plugin = bridge();
    if (!plugin || typeof plugin.downloadModel !== 'function') {
      throw new Error('当前环境不支持下载翻译模型');
    }
    return plugin.downloadModel({ direction });
  },

  /** 取消下载。插件缺席时是空操作。 */
  async cancel() {
    const plugin = bridge();
    if (!plugin || typeof plugin.cancelDownload !== 'function') return;
    try {
      await plugin.cancelDownload();
    } catch (error) {
      console.warn('[native-translate] 取消失败:', error);
    }
  },

  /**
   * 从本地文件导入一个方向的模型：原生侧弹系统文件选择器，用户挑 zip 即可。
   * 用户取消时返回 ``{ ok:false, cancelled:true }``。
   */
  async importZip(direction) {
    const plugin = bridge();
    if (!plugin || typeof plugin.importModel !== 'function') {
      throw new Error('当前环境不支持从文件导入');
    }
    return plugin.importModel({ direction });
  },

  /**
   * 订阅安装进度，返回取消订阅函数。事件形状：
   * ``{direction, phase, received?, total?, entries?, bytes?}``，
   * phase ∈ resolve / download / verify / unzip / done。
   */
  onProgress(handler) {
    const plugin = bridge();
    if (!plugin || typeof plugin.addListener !== 'function' || typeof handler !== 'function') {
      return () => {};
    }
    // Capacitor 7 的 addListener 既能当 handle 用、也能 await，两种都兼容。
    const handle = plugin.addListener('mtProgress', (data) => {
      try {
        handler(data || {});
      } catch (error) {
        console.warn('[native-translate] 进度回调异常:', error);
      }
    });
    return () => {
      Promise.resolve(handle)
        .then((h) => h && typeof h.remove === 'function' && h.remove())
        .catch(() => {});
    };
  },
};
