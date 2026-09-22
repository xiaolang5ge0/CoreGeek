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
2. **定位任务书**：从 `phaseTask` 正则提取文档名，用健壮命令（`find … -iname …` 而非固定路径）定位 `task_*.md`；是否误把 `phaseTask` 文本当参数值（见坑 4）。
3. **读任务书**：是否真正拿到任务书内容（输出里应有 `=== TASK ===` / 原文片段）。
4. **读二级文档（易漏）**：任务书里引用的 `API_DOCS.md` / `spec.md` 是否被**再提取文档名并读取**；只读任务书、不读它引用的文档 = 后面必然靠猜。
5. **分类**：工程修复类 vs API 类；是否走了确定性路径。
6. **确定性执行优先**：工程类是否读 `spec.md` → 修复 → 去 CRLF（`/bin/sh^M`）→ 跑 `check` 拿真实 TOKEN；API 类是否按文档一次性拨对认证×参数。
7. **参数/认证纠错收敛**：是否在 `city`/`location`、`Bearer`/`X-API-Key` 之间反复横跳；**正确组合是否被尝试**（注意别每次只改一个变量、另一个退回旧值）；纠错是否收敛。
8. **收割**：是否从响应中提取到答案字段（如 `total_count` / `world_heritage_count` / `oldest_era`）；**401/400 是否被误解析成“空数据/0”**（假成功，见坑 6）。
9. **提交（最关键）**：拿到答案后是否出现 `submitAnswer`；提交的是**真实 token/答案**还是占位符（如 `{"token":"xxx"}`）；**没有提交 = 任务失败**，即使答案已算出。
10. **时限**：`timeoutRounds`（任务书为 15）内是否完成；超时后 `phaseTask` 清空、`isValid=false`。
11. **LLM 用量与兜底顺序**：任务期 LLM 不限次且不占每日额度，但**确定性优先、LLM 仅兜底**；重点看是否把 LLM 当主路径（逐回合生成 curl/命令）。

## 6. 案例结论（2026-09-22，issue #18 teamB / #19 teamA）

- 两局**任务1（工程修复）成功**：靠 LLM 三次（cat spec → 改配置 → 去 CRLF）才过，确定性路径缺失。
- 两局**任务2（API 查询）未提交**：teamB 在 r36 已算出答案（`total_count=10, world_heritage_count=6, oldest_era=周口店遗址`），但 r37/r38 开拓者只移动/建墙，**无 `submitAnswer`**；teamA 到 r36 才拿到原始 records，r37 同样无提交。
- 结果：两局 `totalScore` 均停在 **88**（仅任务1的 80 分），任务2 疑似超时失败。
- 参数/认证纠错未生效：r26–r35 在 `city`/`location`、`Bearer`/`X-API-Key` 间横跳；teamA 额外浪费 r33–r34（又用回 `X-API-Key` 得空结果）。
- 待办候选（择机进 `PROGRESS.md`）：① 收割后**强制提交**答案；② API 参数/认证纠错收敛策略；③ 禁止把 `phaseTask` 文本当参数值。

### 2026-09-22 第二批（issue #28–#32，改任务逻辑后）

- 5 局全部 **0 分**，两个任务都没过。
- **进步**：`pt → 正则提取文档名 → 健壮命令读任务书` 已稳定（出现 `f=$(find … -iname "task_*.md" …)` + `=== TASK ===`）。
- **新缺口**：读任务书后**没有读它引用的二级文档**（`API_DOCS.md` / `spec.md`），紧接着就 curl / 直接提交。
- **API 类**：r15 起直接 curl，认证×参数逐回合横跳，**从未同时拨对 `Bearer + location`**，15 回合超时；#32 还把 401 解析成 `total_count:0`（假成功）。
- **工程类**：读完任务书后**未读 spec、未跑 check**，直接 `submitAnswer {"token":"xxx"}` → `键值比对不通过 $/token 值不符`。
- 待办候选：④ 任务书引用的二级文档必须读取；⑤ LLM 仅兜底、确定性优先；⑥ 错误响应禁止解析成空数据；⑦ 禁止提交占位 token。

## 7. 已知坑

1. **中文乱码**：控制台 GBK 导致，写 UTF-8 文件再读即可。
2. **`python` 不可用**：本机用 `py`（`python` 为商店别名，静默失败/退出码 9009）。
3. **末条记录被截断**：issue 正文最后一条 `round_record` 的 base64 不完整，解密后 JSON 残缺；工具会标记 `[truncated]` 跳过，其余回合不受影响。
4. **参数污染**：日志里出现 `location=请阅读`，说明把任务书文本误当城市名——分析时优先看这类“值来自 phaseTask”的异常。
5. **只读分析**：分析脚本不得改动决策代码；结论先进本文档/PROGRESS，再走证据驱动变更。
6. **假成功**：解析脚本把 401/400 错误体吞成 `total_count:0 / []`，日志上看起来“查到了但为空”，比直接报错更隐蔽——核对时务必把 `lcr` 原文与解析输出对照。
7. **网络受限时的取数**：Python `urllib` 在本机可能 SSL 失败（`UNEXPECTED_EOF`），改用 `curl.exe`（走系统代理）拉取 issue 正文到本地 JSON，再用 `analyze_issue_log.py --file <json>` 分析。
