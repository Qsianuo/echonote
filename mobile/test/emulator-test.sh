#!/usr/bin/env bash
# 模拟器自动验收：启动 → 装包 → 授权 → 启动 App → 截屏/日志。
# 用法：bash mobile/test/emulator-test.sh [阶段: boot|run|log|screen|all]
set -e
export ANDROID_HOME="F:\\android\\sdk"
export ANDROID_AVD_HOME="F:\\android\\avd"
ADB=/f/android/sdk/platform-tools/adb.exe
EMU=/f/android/sdk/emulator/emulator.exe
APK=/f/echonote/EchoNote-安卓测试版.apk
PKG=com.echonote.app
OUT=/f/echonote/mobile/test/out
mkdir -p "$OUT"

boot() {
  "$EMU" -avd echonote -no-window -no-audio -no-boot-anim -gpu swiftshader_indirect \
    -memory 3072 > "$OUT/emulator.log" 2>&1 &
  echo "等待设备上线..."
  "$ADB" wait-for-device
  until [ "$("$ADB" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; do
    sleep 3
  done
  echo "系统启动完成"
}

run() {
  "$ADB" install -r "$APK"
  # 麦克风：直接授权，跳过运行时弹窗（真机验证弹窗行为，模拟器图省事）
  "$ADB" shell pm grant "$PKG" android.permission.RECORD_AUDIO || true
  "$ADB" logcat -c
  "$ADB" shell am start -n "$PKG/.MainActivity"
  echo "App 已启动，等 25 s 让首启流程跑起来..."
  sleep 25
  "$ADB" exec-out screencap -p > "$OUT/screen-1-launch.png"
  echo "--- Capacitor/插件相关日志 ---"
  "$ADB" logcat -d | grep -iE "capacitor|echonote|sherpa|chromium.*(error|exception)" | tail -40 || true
}

case "${1:-all}" in
  boot) boot ;;
  run) run ;;
  log) "$ADB" logcat -d | grep -iE "capacitor|echonote|sherpa|chromium" | tail -60 ;;
  screen) "$ADB" exec-out screencap -p > "$OUT/screen-$(date +%s).png" && ls "$OUT" ;;
  all) boot && run ;;
esac
