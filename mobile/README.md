# EchoNote 移动端（路线 B）

Capacitor 壳 + 原生 sherpa-onnx 插件：**识别全部在手机本地，首启下载模型后飞行模式可用**。
UI 复用上层 `web/`；后端 FastAPI 完全不参与。

```
mobile/
  package.json / capacitor.config.ts   Capacitor 工程（appId com.echonote.app）
  test/             VAD 双语言一致性测试（npm test；实现在 web/native-vad.js）
  android-plugin/SherpaASRPlugin.kt    安卓插件源码（拷入 android 工程编译）
  ios-plugin/SherpaASRPlugin.swift     iOS 插件源码（需 Mac/Xcode）

web/（随壳一起打包的引擎层，桌面环境加载等于空操作）
  native-vad.js        app/vad.py 的忠实移植（双语言一致性测试守护）
  native-engine.js     采集帧→VAD→流式字幕→SenseVoice 定稿 的编排层
  native-sherpa.js     原生插件封装：模型下载（首启一次）+ base64 PCM 桥
  native-boot.js       自激活适配：拦截 /api/* 与 WebSocket，仅原生壳内生效
```

## 状态（2026-10-24，里程碑 3 · APK 已产出）

- [x] 工程骨架 + 依赖（Capacitor 7，npmmirror）
- [x] VAD 移植 + 与 Python 版逐段对齐的一致性测试（6 组样本全过）
- [x] 引擎编排层（含模式预设、短段重复守卫、增量喂音、flush）
- [x] 双端插件源码
- [x] 传输适配层（native-boot 拦截 fetch/WebSocket，桌面行为不变）
- [x] **安卓构建链全 F 盘**（JDK21=MS OpenJDK、SDK、gradle-home、
      wrapper 走腾讯镜像），首个 debug APK 产出：
      `android/app/build/outputs/apk/debug/app-debug.apk`（70 MB，含
      arm64-v8a + x86_64 的 sherpa-onnx JNI 与全部 web 资产）
- [ ] **里程碑 3 收尾**：真机安装联调（首启模型下载、录音、飞行模式验收）
- [ ] **里程碑 4**：iOS 构建（需 Mac）+ 双端商店素材/合规

### 本机构建命令（备忘）

```bash
cd F:\echonote\mobile\android
set JAVA_HOME=F:\android\jdk-21.0.12.1+1
set ANDROID_HOME=F:\android\sdk
set GRADLE_USER_HOME=F:\android\gradle-home
gradlew.bat assembleDebug
```

改了 web/ 后先 `cd mobile && npx cap sync android` 再构建。

正式包用 `assembleRelease`，产物只有 `app-release.apk`。**AAB 自 2026-10-28 起停发**
（不上架 Google Play，AAB 对侧载与国内商店无用）；真要上架时临时执行
`gradlew.bat bundleRelease`，构建配置未做任何删改。

## 安卓构建说明

构建链已在本机落地（无需 Android Studio，纯命令行）：

- `F:\android\jdk-21.0.12.1+1` — MS OpenJDK 21（Capacitor 7 插件要求 21；
  另有 jdk-17 供 sdkmanager 用）
- `F:\android\sdk` — cmdline-tools + platform-tools + platforms;android-35
  + build-tools 35（wrapper 走腾讯 gradle 镜像）
- `F:\android\gradle-home` — Gradle 缓存（依赖已全部落缓存，二次构建约 1-4 分钟）
- sherpa-onnx v1.13.8：JNI `.so` 在 `app/src/main/jniLibs/`（arm64-v8a +
  x86_64），Kotlin 绑定源码在 `app/src/main/java/com/k2fsa/sherpa/onnx/`
  （来自官方 kotlin-api-examples；**Maven Central 没有 k2-fsa 构件**，
  GitHub Releases 的安卓包只含 .so）

装 Android Studio 仅在需要 IDE 调试/模拟器时才必要（安装时把 SDK 路径
指到 `F:\android\sdk` 即可复用现有组件）。

## iOS 构建（里程碑 4，需 Mac）

1. `npm run add:ios`；
2. 从 sherpa-onnx GitHub Releases 下载
   `sherpa-onnx-v*-*-ios.tar.bz2`，解出的 `sherpa_onnx.xcframework`
   拖进 Xcode 工程（Embed & Sign）；
3. 把 `ios-plugin/SherpaASRPlugin.swift` 加入 App target，在
   ` capacitor.config` 生成的注册处 `registerPlugin`；
4. Info.plist 添加：
   `NSMicrophoneUsageDescription = EchoNote 需要使用麦克风进行本地录音转写`。

## 离线验收（上架前必过）

飞行模式 → 打开 App → 录一句 → 字幕实时出现 → 定稿带标点 → 导出 SRT。

## 已知取舍

- 音频走 Capacitor 桥（base64，~43 KB/s），v1 不做原生采集——采集链路
  （AGC/高通/暂停）全部复用现有 worklet；若桥开销在低端机可见，里程碑 3
  再评估原生 AudioRecord。
- whisper 后端不上手机（ctranslate2 无 ARM 版）；远程后端为可选扩展。
