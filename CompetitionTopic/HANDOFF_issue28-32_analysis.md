# 日志分析交接文档：issue #28–#32（2026-09-22 第三批）

> 用途：把本次「解密分析 + 结论 + 请求时间线」交给下一个会话继续分析/迭代。
> 性质：只读分析，**未改动、未提交任何代码**。
> 生成时间：2026-09-22（会话内）。

---

## 0. 请求时间线（本会话我收到的指令，按序）

| # | 用户请求 | 我做的事 | 是否改代码/提交 |
|---|---|---|---|
| 1 | 分析 issue #18/#19，解密日志，打印每轮自进化任务书交互，先不改代码 | 用 `telemetry.py` 同款密钥流解密，输出逐回合交互 | 否 |
| 2 | 将本次分析流程落盘，用作后续迭代进化 | 新增 `tools/analyze_issue_log.py`、`CompetitionTopic/LOG_ANALYSIS_PLAYBOOK.md`，并在 `PROGRESS.md` 加链接 + 第八轮复盘 | 只加文档/工具，未改决策代码 |
| 3 | 同样分析 issue #23/#24（任务逻辑已改动） | 解密并打印逐回合交互 | 否 |
| 4 | 同样分析 issue #28/#29/#30/#31/#32，并指出：pt 应正则提取文档名→健壮命令读文档，失败才用 LLM 兜底，而不是上来就 curl | 解密 5 局，逐回合交互 + 验证该判断 | 否 |
| 5 | （承接）继续 → 落盘结论 | 在 `LOG_ANALYSIS_PLAYBOOK.md` 补检查清单（读二级文档、确定性优先等）+ `PROGRESS.md` 加第九轮复盘 | 只加文档 |
| 6 | “你提交了吗” | 只提交两份文档，`git push` 因网络失败（未成功） | 本地 commit `abba215`，未 push |
| 7 | 本请求 | 只生成这份 MD 交接文档，不改动/不提交代码 | 否 |

> 备注：工作区另有**用户自己的代码改动** `game/CoreGeek/src/agent/brain.py`、`game/CoreGeek/src/agent/planners/task.py`，本会话全程未触碰。

---

## 1. 解密方法（复用）

- 密钥方案见 `game/CoreGeek/src/agent/telemetry.py`：`LOG_KEY=b"12345678"`；
- 密文 = `base64(明文 XOR keystream)`，`keystream = SHA256(LOG_KEY + counter.to_bytes(4,"big"))` 拼接；
- 日志每回合一行 `round_record <b64>`；
- 一键分析工具：`game/CoreGeek/tools/analyze_issue_log.py`
  - `py tools/analyze_issue_log.py --issue 28 29 30 31 32 --out report.txt`
  - 或 `--file <本地 issue JSON / jsonl.enc>`。
- 本次因 Python `urllib` SSL 失败，改用 `curl.exe`（走系统代理）拉 issue 正文到本地 JSON，再 `--file` 分析。

---

## 2. 本次分析结果

### 2.1 概况

| Issue | 队 | 解密回合 | 任务顺序 | 结果 |
|---|---|---|---|---|
| #28 | teamA (13) | 37/38 | beijing → alpha | 两个任务全失败，score 0 |
| #29 | teamA14 | 40/41 | beijing → alpha | 全失败，score 0 |
| #30 | teamA15 | 39/40 | beijing → alpha | 全失败，score 0 |
| #31 | teamA16 | 39/40 | beijing → alpha | 全失败，score 0 |
| #32 | teamB11 | 38/39 | beijing → alpha | 全失败，score 0 |

每局末尾各有 1 条记录在 issue 正文中被截断（JSON 残缺），已跳过，不影响其它回合。

---

### 2.2 API 任务（beijing）逐回合

**#28 / #29 / #30 / #31（teamA 系，剧本几乎一致）**

| 回合 | 开拓者动作 / LLM CMD | 结果 |
|---|---|---|
| r12 | `acceptTask` | — |
| r13 | **健壮定位**：`f=$(find /tmp/selfEvolutionTask -iname "task_1_beijing.md" …); d=$(dirname "$f")…` | r14 → `__FILE:…/1-unknown-api/task_1_beijing.md` + `=== TASK ===` + 任务书原文 ✅ |
| r15 | **直接 curl**：`X-API-Key: heritage-api-key-2024 …?city=北京&limit=100` | 401 `Missing 'Authorization' header` |
| r17 | `Authorization: Bearer … ?city=北京` | 400 `Missing required parameter: location` |
| r19 | `X-API-Key … ?location=北京` | 401 |
| r21 | `Bearer … ?city=北京` | 400 |
| r23 | `X-API-Key … ?location=北京` | 401 |
| r25 | `Bearer … ?city=北京` | 400 |
| r27 | `X-API-Key … ?location=北京` | 401 |
| r28 | （上轮结果）+ `错误:[[1,'timeout']]` | **任务超时失败** |

> #28 与 #29/#30/#31 的差异仅在 `limit` 参数（100 / 1000 / 无）与个别回合是否带解析脚本；认证×参数横跳模式相同。

**#32（teamB11）** —— 多套了一层解析脚本，出现“假成功”：

| 回合 | 动作 | 结果 |
|---|---|---|
| r14 | `acceptTask` | — |
| r15 | 健壮定位 `find … task_1_beijing.md` | r16 → `__FILE` + `=== TASK ===` ✅ |
| r17 | `X-API-Key …?city=北京&limit=1000 \| python3 -c "…"` | r18 → `{"city":"北京","total_count":0,"world_heritage_count":0,"types":[],"oldest_era":"Unknown"}` ← **401 被吞成 0，假成功** |
| r19 | 同款解析 | r20 → `Found 0 items` + 全 0 |
| r21 | `X-API-Key … -o /tmp/beijing_raw.json && cat` | r22 → 401 |
| r23 | `Bearer … -o` | r24 → 400 location |
| r25 | `X-API-Key … -o` | r26 → 401 |
| r27 | `Bearer … -o` | r28 → 400 |
| r29 | `X-API-Key … ?location=北京 -o /tmp/beijing_data.json` | r30 → 401 + `timeout` **失败** |

**共同点**：认证（`Bearer`/`X-API-Key`）与参数（`city`/`location`）逐回合横跳，**从未同时拨对 `Bearer + location`**；全程无 `200`；无独立读取 `API_DOCS.md` 的命令。

---

### 2.3 工程任务（alpha）逐回合

**5 局同一剧本：**

| 回合 | 开拓者动作 | 结果 |
|---|---|---|
| r30（#32 为 r33） | `acceptTask` | — |
| r31（r34） | **健壮定位** `find … task_1_alpha.md …` | 下一回合 → `__FILE:…/2-engineering-fix/task_1_alpha.md` + `=== TASK ===` ✅ |
| r32（r35） | **立即 `submitAnswer {"token":"xxx"}`**（未读 `spec.md`、未跑 `check`） | r33（r36）→ `错误:[[2,'键值比对不通过: $/token: 值不符']]` |
| 之后 | 仅 move/build，无任务命令 | 任务失败 |

---

## 3. 关键结论

| 环节 | 期望 | 实际（#28–#32） |
|---|---|---|
| pt → 正则提取文档名 | 取 `task_1_beijing.md` | ✅ 已做到（健壮 `find -iname`） |
| 读任务书 | 读到内容 | ✅ 已做到（输出 `=== TASK ===`） |
| 读任务书**引用的二级文档** | 再取 `API_DOCS.md` / `spec.md` 并读取 | ❌ 缺失 |
| 按文档确定性执行 | 认证×参数一次拨对；工程类修复 + check | ❌ 直接 curl / 直接提交假 token |
| LLM 兜底顺序 | 仅在确定性路径失败时兜底 | ❌ LLM 成了主路径，逐回合猜，横跳至超时 |
| 错误处理 | 401/400 识别为失败 | ❌ #32 把 401 吞成 `total_count:0`（假成功） |

**一句话**：`读任务书` 已稳定，但 `顺着任务书去读它引用的文档、再按文档确定性执行` 没做；LLM 兜底被提前当主路径用，导致 API 任务横跳超时、工程任务提交占位 token。

---

## 4. 与前几批对比（趋势）

| 批次 | issue | 工程任务 | API 任务 | 分数 |
|---|---|---|---|---|
| 第一批 | #18/#19 | 成功（LLM 3 次：cat spec→改配置→去 CRLF） | 已算出答案但**未提交** | 88 |
| 第二批 | #23/#24 | teamA 成功 / teamB 因漏 `cd` 失败 | **读完文档后停摆**，未 curl | teamA 88 / teamB 0 |
| 第三批 | #28–#32 | **未读 spec/check，提交占位 token** | **上来就 curl、横跳超时** | 全 0 |

---

## 5. 待交接给下一会话的验收点

1. **API 任务**：日志中必须出现读取 `API_DOCS.md` 的命令，并一次性命中 `Bearer + location` 返回 `200`。
2. **工程任务**：必须出现读取 `spec.md` + 跑 `check` 拿到真实 TOKEN 后再 `submitAnswer`；禁止占位/猜测答案。
3. **纠错收敛**：参数/认证组合要遍历（勿每次只改一个、另一个退回旧值），命中 `200` 即锁定。
4. **错误处理**：非 2xx / `status:error` 禁止解析成 `0/空`；显式失败。
5. **兜底顺序**：确定性优先，LLM 仅在读文档/解析失败时兜底。

---

## 6. 附：产物与注意事项

- 分析工具：`game/CoreGeek/tools/analyze_issue_log.py`（已存在于仓库）。
- SOP：`CompetitionTopic/LOG_ANALYSIS_PLAYBOOK.md`（已补检查清单与坑）。
- 复盘：`CompetitionTopic/PROGRESS.md` 第八/九轮。
- 本地未推送提交：`abba215 docs: issue#28-#32 日志复盘…`（`git push` 因网络被重置失败；如需保留请自行 push）。
- 网络注意：Python `urllib` 可能 SSL 失败，用 `curl.exe` 走代理取数。
- 控制台中文乱码是 GBK 显示问题，报告以 UTF-8 落盘即可。
