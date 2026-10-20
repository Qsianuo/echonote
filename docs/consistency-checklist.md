# GATE-0 文档一致性清单

> 用途：PRD v1.1 §15.2 GATE-0 的证据物。每一项都必须能用命令复现，**不接受“我看过了”**。
> 代码基线：本次实现（文档 v1.1 + 产品 v0.9.0 tech preview）。

## 0. 断言口径说明（一处 PRD 内部冲突的处理）

PRD GATE-0 写的是 `! grep -rn "3.9" README.md start.bat`，但 D-02 又要求 README **显式声明 3.9 不支持**（3.9 已于 2025-10 EOL）。
两条同时满足不可能。本清单采用能表达意图的断言：

```bash
# 断言“不再宣称支持 3.9”，而不是“永不出现 3.9 这个数字”
grep -rn "3\.9+" README.md start.bat run.sh app/     # 必须为空
```

并把“不支持 3.9”作为显式文案保留（README「开发与测试」段 + `start.bat` 版本检查）。

## 1. 逐项结论

| # | 检查项 | 命令 | 期望 | 实测（本次） |
|---|--------|------|------|--------------|
| 1 | 不宣称 Python 3.9 支持 | `grep -rn "3\.9+" README.md start.bat run.sh app/` | 空 | ✅ 空 |
| 2 | 不写“1–2 秒”延迟承诺 | `grep -rn "1–2 秒\|1-2 秒" README.md web/` | 空 | ✅ 空（改为 A/B/C 分档 p95） |
| 3 | Worklet 分片口径正确 | `grep -rn "40–90 ms\|40-90 ms" README.md web/` | 空 | ✅ 空（实际 8 ms/块，上行合并到 100 ms） |
| 4 | 导出数量口径 | `grep -rn "三种文本导出" README.md` | 空 | ✅ 空（四种导出：TXT/MD/SRT/WAV） |
| 5 | 产品版本号与阶段一致 | `grep -rn "version=" app/main.py` | `config.APP_VERSION` | ✅ `version=config.APP_VERSION` → `0.9.0` |
| 6 | 前端不以 health 做门控 | `grep -rn "api/health" web/` | 空 | ✅ 空（只用 `/api/ready`） |
| 7 | VAD 取整已披露 | `grep -n "900 ms" README.md` | 命中 | ✅ README 配置表：900 → 实际 900 ms |
| 8 | 最短语音已披露 | `grep -n "240 ms" README.md` | 命中 | ✅ README 配置表：240 → 实际 240 ms |
| 9 | 灵敏度方向无歧义 | `grep -n "越不灵敏" README.md` | 命中 | ✅ “数值越大越不灵敏（更难触发）” |
| 10 | 新增配置已文档化 | `grep -n "ECHONOTE_MAX_RECORD_MINUTES\|ECHONOTE_ALLOW_NONLOCAL" README.md` | 各命中 | ✅ 均在配置表 |
| 11 | 探针语义已写明 | `grep -n "仅表示进程存活" README.md` | 命中 | ✅ `/api/health` 行 |
| 12 | 前端无 `innerHTML` 渲染 | `grep -rn "innerHTML" web/` | 空 | ✅ 空（全部 `textContent`） |

## 2. 代码侧一致性（与文案对齐的实现点）

| 项 | 位置 | 值 |
|----|------|----|
| 协议版本 | `app/protocol.py` `PROTOCOL_VERSION` | `"2"` |
| 单帧上限 | `app/config.py` `MAX_FRAME_BYTES` | `32768` |
| 上行帧长 | `app/config.py` `MIN_FRAME_MS` / `web/app.js` `MERGE_SAMPLES` | 100 ms / 1600 样本 |
| 队列上限 | `app/config.py` `AUDIO_QUEUE_MAX_SECONDS` | 60 s |
| 并发上限 | `app/config.py` `MAX_CONCURRENT_SESSIONS` | 2 |
| 软上限 | `app/config.py` `MAX_RECORD_MINUTES` | 90（0=不限） |
| 内存预算 | `app/config.py` `RECORDING_MEMORY_BUDGET_BYTES` | 200 MiB |
| VAD 帧数 | `app/vad.py` | hangover 30 帧 = 900 ms；min_speech 8 帧 = 240 ms |

## 3. 已知不一致 / 待补

| 项 | 状态 | 处置 |
|----|------|------|
| `docs/screenshot.png` 仍是旧界面（无导出菜单焦点态、无 90 分钟提示） | 待重出 | 发布前按 GATE-0 第 7 项重出截图，标 `small` + 停止态 |
| 文档版本 v1.1 ≠ 产品版本 v0.9.0 | 已按 D-02/§0.2 分离 | 对外只写 `v0.9.0 tech preview` |
| `on_event("startup")` 弃用 | 已修 | 改用 `lifespan`（原 NFR-CI-04 债务项之一） |
| 无 `pyproject.toml` / lint / 类型检查 | 未处理 | 保留在 Backlog（P2） |

## 4. 复现方式

```bash
bash docs/gate0-check.sh      # 逐条打印期望值并给出 PASS/FAIL
```

> 任一 FAIL 即 GATE-0 未通过；没有执行记录按“未验证”处理。
