#!/usr/bin/env bash
# GATE-0 consistency gate (PRD v1.1 §15.2).
# Usage: bash docs/gate0-check.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

fail=0

expect_empty() {
  local name="$1" pattern="$2"
  shift 2
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
expect_empty "20 no false 'not written to disk' claim" "不写入磁盘"        README.md web/

if [ "$fail" -eq 0 ]; then
  echo "----"
  echo "GATE-0: PASS (all checks)"
else
  echo "----"
  echo "GATE-0: FAIL"
fi

exit "$fail"
