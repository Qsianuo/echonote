# EchoNote 隐私政策 / Privacy Policy

**生效日期：2026-10-24 | 最后更新：2026-10-24**

EchoNote 声记（以下简称"本应用"）是一款本地录音与实时语音转写工具。我们以最严格的口径承诺：**你的声音和文字，不离开你的设备。**

## 一、我们收集什么数据

**答案：无。**

- **录音音频**：仅保存在你自己设备的本地存储（浏览器 OPFS 或应用沙盒），由你手动导出或删除。本应用没有任何上传音频的代码路径。
- **转写文本**：仅保存在设备本地，随录音一并导出或清除。
- **语音识别**：完全在本设备上的本地模型（sherpa-onnx / SenseVoice）中完成，不经过任何服务器。
- **账号系统**：本应用没有账号、没有登录、没有用户画像。
- **广告与追踪**：无广告 SDK、无统计 SDK、无崩溃上报 SDK。

## 二、本应用什么时候会联网

仅两种情况，且只传输下列数据：

1. **首次启动的模型下载**：从 Hugging Face 镜像（hf-mirror.com）下载开源语音识别模型文件（约 270 MB）。传输内容只有模型文件本身，不包含你的任何数据。下载完成后，离线即可使用全部功能。
2. **可选的远程识别后端**（默认关闭）：仅当用户显式填写第三方接口地址与密钥后才启用；此时音频按用户指示发送到用户指定的服务。默认安装状态下此功能完全关闭。

## 三、权限说明

| 权限 | 用途 |
|---|---|
| 麦克风（RECORD_AUDIO） | 仅在用户主动点击录音时采集音频，用于本地转写 |
| 网络（INTERNET） | 仅用于首次模型下载与用户显式开启的远程识别 |

## 四、数据存储与删除

- 录音与转写保存在设备本地应用沙盒中。
- 用户可随时在应用内"清空文本"、"删除录音"或卸载应用；卸载即彻底删除全部数据。

## 五、未成年人

本应用不收集任何数据，不设年龄限制，但建议在成人指导下使用。

## 六、政策变更

若未来版本引入任何数据收集行为，将在应用内显著位置提示并更新本政策。

## 七、联系方式

如有疑问，请通过 GitHub 仓库 Issues 联系：https://github.com/Qsianuo/echonote/issues

---

# Privacy Policy (English)

**Effective: 2026-10-24**

EchoNote is a local recording and real-time speech-to-text tool. **Your audio and transcripts never leave your device.**

**What we collect: nothing.** Audio and transcripts are stored only in your device's local app storage. Speech recognition runs entirely on-device using local models. There are no accounts, no ads, no analytics, no crash reporting, no tracking SDKs.

**Network access** is used only for (1) a one-time download of open-source recognition models (~270 MB) from the Hugging Face mirror, containing no user data, and (2) an optional, off-by-default remote backend that sends audio only to a service endpoint the user explicitly configures.

**Permissions**: microphone (used only while the user is actively recording) and network (model download / optional backend).

**Data deletion**: uninstalling the app removes all data permanently.

**Contact**: https://github.com/Qsianuo/echonote/issues
