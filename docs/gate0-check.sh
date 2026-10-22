#!/usr/bin/env bash
# GATE-0 consistency gate (PRD v1.1 §15.2).
# Usage: bash docs/gate0-check.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

fail=0
checks=0

expect_empty() {
  local name="$1" pattern="$2"
  shift 2
  checks=$((checks + 1))
  local out
  out="$(grep -rn "$pattern" "$@" 2>/dev/null || true)"
  if [ -z "$out" ]; then
    printf 'PASS  %s\n' "$name"
  else
    printf 'FAIL  %s\n' "$name"
    printf '%s\n' "$out" | sed 's/^/      /'
    fail=1
  fi
}

expect_hit() {
  local name="$1" pattern="$2"
  shift 2
  checks=$((checks + 1))
  if grep -rq "$pattern" "$@" 2>/dev/null; then
    printf 'PASS  %s\n' "$name"
  else
    printf 'FAIL  %s\n' "$name"
    fail=1
  fi
}

expect_empty "1  no 3.9+ support claim"          "3\.9+"                README.md start.bat run.sh app/
expect_empty "2  no 1-2s latency promise"        "1–2 秒\|1-2 秒"       README.md web/
expect_empty "3  no 40-90ms worklet chunk claim" "40–90 ms\|40-90 ms"   README.md web/
expect_empty "4  no 'three text exports'"        "三种文本导出"          README.md
expect_empty "6  frontend never gates on health" "api/health"           web/
expect_empty "12 no innerHTML rendering"         "innerHTML"            web/
expect_empty "14 no download in the request path" "snapshot_download"   app/main.py app/stt.py app/desktop.py

expect_hit "5  version comes from config"        "version=config.APP_VERSION" app/main.py
expect_hit "7  hangover rounding disclosed"      "900 ms"               README.md
expect_hit "8  min-speech rounding disclosed"    "240 ms"               README.md
expect_hit "9  sensitivity direction is explicit" "越不灵敏"             README.md
expect_hit "10 max record minutes documented"    "ECHONOTE_MAX_RECORD_MINUTES" README.md
expect_hit "10 non-local opt-in documented"      "ECHONOTE_ALLOW_NONLOCAL"     README.md
expect_hit "11 liveness probe semantics stated"  "仅表示进程存活"        README.md
expect_hit "13 protocol v4 documented"           '"protocol":"4"'       README.md
expect_hit "15 offline mode documented"          "ECHONOTE_OFFLINE"     README.md
expect_hit "16 model switch documented"          "识别模型"              README.md
expect_empty "17 audioChunks fully removed"      "audioChunks"            web/
expect_hit   "18 preroll documented"             "ECHONOTE_VAD_PREROLL_MS" README.md
expect_hit   "19 timing message documented"      '"type":"timing"'        README.md
expect_empty "20 no false 'audio not written to disk' claim" "\(录音\|音频\|语音\).\{0,10\}不写入磁盘" README.md web/
# #20 的距离窗口是 10 字，拦不住"录音与识别均在本机完成，不上传云端，也不写入磁盘"
# 这种拉开 21 字的原句。放宽距离会误伤"转写文本不写入磁盘"之类正确表述，
# 所以单独钉住这句原话。
expect_empty "20b no 'not uploaded, not written to disk' sentence" "不上传云端，也不写入磁盘" README.md web/

checks=$((checks + 1))
fe_proto=$(sed -n "s/.*PROTOCOL_VERSION = '\([0-9]*\)'.*/\1/p" web/app.js | head -1)
sv_proto=$(sed -n 's/.*PROTOCOL_VERSION = "\([0-9]*\)".*/\1/p' app/protocol.py | head -1)
if [ -n "$fe_proto" ] && [ "$fe_proto" = "$sv_proto" ]; then
  printf 'PASS  %s\n' "21 protocol constant in sync ($fe_proto)"
else
  printf 'FAIL  %s\n' "21 protocol constant mismatch: web=$fe_proto server=$sv_proto"
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  echo "----"
  echo "GATE-0: PASS ($checks checks)"
else
  echo "----"
  echo "GATE-0: FAIL"
fi

exit "$fail"
