# EchoNote 前端架构（v1.0.0）

> 本文是 `web/` 目录的分层地图：现状、分层契约、以及下一阶段（app.js 模块化）的计划。
> 产出背景：v1.0.0 框架评审——前端基于 HTML/JS（桌面 = 同一页面套 Edge `--app`
> 窗口；Android = 同一页面套 Capacitor WebView），采集与识别链路已有模块化雏形，
> 但 **app.js 是 2602 行单体** 且原生集成靠全局猴子补丁。

## 一、分层现状

```
┌─ 界面层（app.js，2602 行 IIFE 单体）────────────────────────────┐
│ 状态机 state · DOM 注册表 el · 录音生命周期 · 字幕渲染 ·          │
│ 状态/错误提示 · 快捷键 · 导出菜单 · 回放 UI · 模式切换            │
└──────────────┬───────────────────────────────────────────────┘
               │ window.* 全局（Playback / Expo / Audio / wf …）
┌─ 领域模块层（独立文件 + Node 测试）──────────────────────────────┐
│ playback.js（播放预排）· wav.js（WAV 编解码）· waveform.js        │
│ audio-store.js + storage-worker.js（OPFS 落盘）· export.js（导出）│
│ pcm-worklet.js（采集 DSP：高通/陷波/抗混叠/AGC/重采样）           │
└──────────────┬───────────────────────────────────────────────┘
               │
┌─ 传输层 ──────────────────────────────────────────────────────┐
│ transport.js（v1.0.0 新增）：EchoNoteTransport 契约              │
│   · server 会话 = 真 WebSocket（桌面/局域网，令牌由本层注入）      │
│   · native 会话 = NativeSession 适配器（Capacitor 桥）            │
│   · api() = /api/* 请求：server 透传 fetch；native 本地应答        │
└──────────────┬───────────────────────┬───────────────────────┘
               │                       │
      Python 服务（8000）      native-boot.js + MobileEngine
      Faster-whisper/zipformer/  + sherpa-onnx 原生插件
      SenseVoice（三引擎）       （手机本地，完全离线）
```

## 二、两条环境的契约（transport.js）

| 契约 | server（桌面/局域网） | native（Android/iOS 壳） |
|---|---|---|
| `openSession()` | 真 WebSocket（`/ws` + 局域网令牌） | `NativeSession`（事件由 native-boot 驱动） |
| `session.send(string)` | 发 JSON 帧（config/flush） | `onJSON` → 引擎指令 |
| `session.send(ArrayBuffer)` | 发 16kHz PCM 帧 | `onAudio` → 引擎喂帧 |
| `api('/api/…')` | 透传 fetch 到 FastAPI | 壳内本地应答（config/ready/mode/retry） |
| 会话内消息 | 服务端协议（partial/final/refine/done/warning/status） | 同形状（MobileEngine 产出） |

**收益**：原生壳不再替换 `window.WebSocket`、不再拦截 `fetch`——集成点从
"全局猴子补丁"变为"一个注册调用"，桌面行为与原生行为的分界一目了然。

## 三、已知的结构债（下一阶段）

1. **app.js 单体**（2602 行）：state/el 与录音生命周期、渲染、快捷键同闭包。
   已规划按域拆分为 ES modules（与 native-*.js 同一加载方式）：

   ```
   web/js/state.js      state + el 注册表 + 常量
   web/js/capture.js    getUserMedia / worklet / handlePcm / ledger / store
   web/js/recorder.js   start/stop/pause/resume + 时长/上限/计时
   web/js/session.js    会话生命周期（connect/重连/epoch/origin）
   web/js/transcript.js finals/partial 渲染与去重
   web/js/status.js     applyStatus / 胶囊 / 模型标签
   web/js/ui.js         hint/notice/error/announce/对话框
   web/js/keyboard.js   空格与方向键仲裁
   main.js（原 app.js 尾部） boot 与装配
   ```

   风险与对策：全量交互（含真机麦克风）无法在无头环境完整回归，因此拆分
   按「先叶子后主干」推进，每步跑 tests/js + 模拟器 demo 端到端；app.js 单
   体在其间保持可用。

2. ~~**双版本号**：`ASSET_VERSION`（资源缓存）与 `APP_VERSION`（config.py）需
   同步递增，靠人工记忆——Stage 2 顺带收敛到一处。~~
   **已于 2026-10-25 收敛**：`app.js` 的 `ASSET_VERSION`、`index.html` 的 `?v=`、
   `native-boot.js` / `native-engine.js` 的 import specifier 现在全部由
   `tools/bump_asset_version.py` 按**资源内容哈希**生成（一条命令重写全部载体），
   `tests/test_asset_version.py` 兜住漂移。顺带修掉一个隐性 bug：两个原生模块曾
   用不同 `?v=` 引入 `native-vad.js`，浏览器会当成两个模块（两份 `MODES`、两个
   `Segmenter`）。仍需人工维护的只剩 `APP_VERSION`（用户可见版本号）。

3. **全局命名空间**：领域模块用 window 全局通信（历史模式），Stage 2 拆分
   时一并改为 ES imports。

## 四、测试口径

- `tests/js/`：pcm-worklet（含块边界零填充回归、降噪链）、audio-store、
  export、playback、wav、waveform——纯逻辑层
- `mobile/test/`：VAD 双语言一致性（Python ↔ JS 逐段对齐）
- 端到端：模拟器 demo 音源全流程（下载→就绪→录音→转写→导出）
