# 实战遥测日志分析流程（LOG ANALYSIS PLAYBOOK）

> **本文档 = 可复用的日志分析 SOP**。用于每次 PK 结束后，从加密遥测日志中还原
> 「自进化任务书」的逐回合交互，定位失败根因，喂给后续迭代。
> 配套：`自进化策略.md`（任务系统逻辑）、`PROGRESS.md`（进度/待办）、
> 工具 `game/CoreGeek/tools/decrypt_log.py` 与 `game/CoreGeek/tools/analyze_issue_log.py`。

---

## 1. 数据来源

| 来源 | 说明 |
|---|---|
| GitHub Issue 正文 | 平台 stdout 回显，每回合一行 `round_record <base64>`（如 issue #18/#19） |
| `logs/match_YYYYMMDD_HHMMSS.jsonl.enc` | 本地落盘，每行一条 base64（无 `round_record` 前缀） |
| 平台 stdout | 与 issue 正文同源 |

## 2. 加密格式（勿改，解谜用）

来自 `src/agent/telemetry.py`：

- 明文 = `json.dumps(compact_record(...), ensure_ascii=False)`；
- 密文 = `base64(明文 XOR keystream)`；
- `keystream = SHA256(b"12345678" + counter.to_bytes(4,"big"))` 逐块拼接、截断；
- 密钥常量 `LOG_KEY = b"12345678"`。

> 解密实现见 `tools/decrypt_log.py`（原版）与 `tools/analyze_issue_log.py`（分析增强版）。

## 3. 一键分析流程

```powershell
# Windows 用 py 启动器（本机 python 可能是商店别名，直接用会 9009/静默失败）
py game/CoreGeek/tools/analyze_issue_log.py --issue 18 19 --out task_report.txt

# 或分析本地 .jsonl.enc
py game/CoreGeek/tools/analyze_issue_log.py --file logs/match_20260922_061319.jsonl.enc --out task_report.txt
```

产出 `task_report.txt`（UTF-8），内容为每个任务相关回合的 phaseTask / 指令 / executeCmd / LLM 回复 / 命令结果。

**必须写文件再读**：PowerShell 控制台默认 GBK，直接 print 中文会变 `���Ķ�` 乱码，不代表解密失败。

## 4. 字段字典（`compact_record`）

| 键 | 含义 | 截断 |
|---|---|---|
| `r` | 回合号 | — |
| `g` / `sc` | 金币 / 总分 | — |
| `pt` | `phaseTask` 任务书原文 | 120 |
| `c` | `roleCommandMap` 精简：`{id%1000: [action, name?, targets?, num?, ctl?, taskAnswer?]}` | — |
| `xcmd` | `executeCmd`（开拓者沙盒命令） | 200 |
| `llm` | `llmResp`（LLM 返回） | 200 |
| `lcr` | `lastCmdResult`（上轮命令输出） | 300 |
| `fb_fail` | 上轮失败动作列表 | — |
| `err` | `errors`（errorCode+描述） | 40 |
| `u` / `b` / `z` | 我方角色 / 机器人 / 中立点 | — |

任务相关动作：`acceptTask` / `executeCmd` / `submitAnswer`（出现在 `c` 中，通常是开拓者 id）。

## 5. 自进化任务分析检查清单

按回合还原后逐项核对：

1. **接取**：`acceptTask` 是否成功、是否出现 `phaseTask`；FAIL 是否触发终身回避（防封号）。
2. **定位**：LOCATE 是否找到 `task_*.md` 与 `API_DOCS.md`；是否误把 `phaseTask` 文本当参数值（见坑 4）。
3. **分类**：工程修复类 vs API 类；是否走了确定性路径。
4. **执行**：`executeCmd` 是否有效；工程类是否处理 CRLF（`/bin/sh^M`）；API 类是否命中正确的 URL/路径。
5. **参数/认证纠错**：是否在 `city`/`location`、`Bearer`/`X-API-Key` 之间反复横跳；纠错是否收敛。
6. **收割**：是否从响应中提取到答案字段（如 `total_count` / `world_heritage_count` / `oldest_era`）。
7. **提交（最关键）**：拿到答案后是否出现 `submitAnswer`；**没有提交 = 任务失败**，即使答案已算出。
8. **时限**：`timeoutRounds`（任务书为 15）内是否完成；超时后 `phaseTask` 清空、`isValid=false`。
9. **LLM 用量**：任务期 LLM 不限次且不占每日额度，但应尽量确定性优先、LLM 仅兜底。

## 6. 案例结论（2026-09-22，issue #18 teamB / #19 teamA）

- 两局**任务1（工程修复）成功**：靠 LLM 三次（cat spec → 改配置 → 去 CRLF）才过，确定性路径缺失。
- 两局**任务2（API 查询）未提交**：teamB 在 r36 已算出答案（`total_count=10, world_heritage_count=6, oldest_era=周口店遗址`），但 r37/r38 开拓者只移动/建墙，**无 `submitAnswer`**；teamA 到 r36 才拿到原始 records，r37 同样无提交。
- 结果：两局 `totalScore` 均停在 **88**（仅任务1的 80 分），任务2 疑似超时失败。
- 参数/认证纠错未生效：r26–r35 在 `city`/`location`、`Bearer`/`X-API-Key` 间横跳；teamA 额外浪费 r33–r34（又用回 `X-API-Key` 得空结果）。
- 待办候选（择机进 `PROGRESS.md`）：① 收割后**强制提交**答案；② API 参数/认证纠错收敛策略；③ 禁止把 `phaseTask` 文本当参数值。

## 7. 已知坑

1. **中文乱码**：控制台 GBK 导致，写 UTF-8 文件再读即可。
2. **`python` 不可用**：本机用 `py`（`python` 为商店别名，静默失败/退出码 9009）。
3. **末条记录被截断**：issue 正文最后一条 `round_record` 的 base64 不完整，解密后 JSON 残缺；工具会标记 `[truncated]` 跳过，其余回合不受影响。
4. **参数污染**：日志里出现 `location=请阅读`，说明把任务书文本误当城市名——分析时优先看这类“值来自 phaseTask”的异常。
5. **只读分析**：分析脚本不得改动决策代码；结论先进本文档/PROGRESS，再走证据驱动变更。
