/**
 * EchoNote 传输层（v1.0.0 架构）。
 *
 * 前端有两条运行环境：桌面/局域网（识别在 Python 服务）与原生壳
 * （识别在手机本地的 sherpa-onnx 插件）。此前原生壳通过**全局猴子补丁**
 * 接入——替换 window.WebSocket、拦截 fetch——行为正确但不可见，是整个
 * 前端最"魔法"的部分。本模块把两条环境收敛为一个显式契约：
 *
 *   EchoNoteTransport.openSession(listeners) → session
 *     session：{ addEventListener, removeEventListener, readyState,
 *                send(json|ArrayBuffer), close() }
 *     与 WebSocket 同面，app.js 无需关心底下是谁。
 *   EchoNoteTransport.api(url, init) → Promise<Response>
 *     /api/* 请求：server 走 fetch；native 由壳内本地应答。
 *   EchoNoteTransport.registerNative(impl)
 *     原生壳（native-boot.js）在启动时注册 impl：
 *     { onSessionOpen, onJSON, onAudio, closeSession, api }。
 *
 * 普通 script（非 module）：必须在 app.js 之前加载。
 */
(function (root) {
  'use strict';

  const CONNECTING = 0;
  const OPEN = 1;
  const CLOSING = 2;
  const CLOSED = 3;

  // 原生壳实现，由 native-boot.js 通过 registerNative 注入。
  let nativeImpl = null;

  /** 服务端会话：直接以浏览器 WebSocket 充当（表面完全一致）。
   *  app.js 拿到后自己挂 open/message/close 监听，无需转发层。 */
  function openServerSession() {
    const token = localStorage.getItem('echonote-token');
    const query = token ? `?token=${encodeURIComponent(token)}` : '';
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return new WebSocket(`${proto}//${location.host}/ws${query}`);
  }

  /**
   * 原生会话：与 WebSocket 同面的轻适配器。事件由 native-boot 注入的
   * impl 驱动（onSessionOpen / onJSON / onAudio / closeSession）。
   */
  class NativeSession {
    constructor() {
      this.readyState = OPEN;
      this.binaryType = 'arraybuffer';
      this._listeners = {};
    }

    addEventListener(type, fn) {
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    }

    removeEventListener(type, fn) {
      const list = this._listeners[type] || [];
      const i = list.indexOf(fn);
      if (i >= 0) list.splice(i, 1);
    }

    _emit(type, event) {
      for (const fn of this._listeners[type] || []) {
        try {
          fn({ target: this, ...event });
        } catch (error) {
          console.error('[transport] listener failed:', error);
        }
      }
    }

    send(data) {
      if (typeof data === 'string') nativeImpl.onJSON(data);
      else if (data instanceof ArrayBuffer) nativeImpl.onAudio(data);
    }

    close() {
      if (this.readyState === CLOSED) return;
      this.readyState = CLOSED;
      nativeImpl.closeSession();
      this._emit('close', { target: this });
    }
  }

  root.EchoNoteTransport = {
    CONNECTING,
    OPEN,
    CLOSING,
    CLOSED,

    get mode() {
      return nativeImpl ? 'native' : 'server';
    },

    /** 原生壳启动时注册实现（详见 native-boot.js）。 */
    registerNative(impl) {
      nativeImpl = impl;
    },

    /**
     * 打开一次识别会话。listeners: { onOpen, onMessage(data), onClose, onError }，
     * 其中 onMessage 的 data 与 WebSocket 一致（JSON 字符串或 ArrayBuffer）。
     */
    openSession(listeners) {
      listeners = listeners || {};
      if (nativeImpl) {
        const session = new NativeSession();
        // open 异步派发：调用方在拿到会话后才挂监听器。顺手让壳补发
        // 一次当前状态（install 时的状态推送那时还没有监听者）。
        queueMicrotask(() => {
          if (nativeImpl.onSessionOpen) nativeImpl.onSessionOpen();
          session._emit('open', { target: session });
        });
        return session;
      }
      return openServerSession();
    },

    /** /api/* 请求：server 透传 fetch，native 由壳内本地应答。 */
    api(url, init) {
      if (nativeImpl) return nativeImpl.api(url, init);
      return fetch(url, init);
    },
  };
})(window);
