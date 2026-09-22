"""L3 TaskPlanner：自进化任务求解器 v2（参照《自进化策略.md》实战版重构）。

核心原则（来自真实 PK 经验）：
1. acceptTask FAIL 立即放弃不重试（FAIL 计 errorCode 4，5 次异常封号）——由 PioneerFSM 执行
2. 确定性 LOCATE 优先于 LLM：find 任务文档 + cat 全部 .md（__FILE/__DIR/__DOC 标记）
3. 任务分类处理：工程修复类（_ws 确定性修复）/ API 类（harvest 探测）/ 通用 LLM
4. LLM 严格单行协议：CMD: <命令> 或 ANSWER: <答案>
5. 防振荡（LLM 循环≥4 提交保底）、命令失败计数、Multi Submit
6. 跨任务经验：persisted_api_facts（认证头/参数名）
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field

from ..protocol import Turn, submit_answer_command

# ---- 确定性命令模板 ----
LOCATE_CMD = (
    'f=$(ls task_*.md 2>/dev/null | head -1); '
    '[ -z "$f" ] && f=$(find /tmp /home /workspace /root /data -maxdepth 5 -name "task_*.md" 2>/dev/null | head -1); '
    'd=$(dirname "$f"); echo "__FILE:$f"; echo "__DIR:$d"; '
    'for x in "$d"/*.md; do echo "__DOC:$x"; cat "$x"; done; echo "__END"'
)

WS_PROBE_CMD = (
    'cd "{dir}" && find . -maxdepth 2 | head -40; '
    'echo "__SPEC__"; cat spec.md 2>/dev/null; echo "__CHECK__"; '
    "sed -i 's/\\r$//' check 2>/dev/null; chmod +x check 2>/dev/null; ./check"
)

# API 收割脚本 v2（探测 认证×路径×参数名×城市；400 错误信息作为事实输出）
# 实战教训：服务端要 Authorization: Bearer + 参数名是 location 而非 city；
# 中文城市名从任务原文（TASK_B64）与文档中提取候选，逐一实测。
HARVEST_PY = r'''
import base64, json, re, glob, urllib.request, urllib.error, urllib.parse

TASK_TEXT = base64.b64decode("__TASK_B64__").decode("utf-8", "ignore")
docs = " ".join(open(f, encoding="utf-8", errors="ignore").read() for f in glob.glob("**/*.md", recursive=True) + glob.glob("*.md"))
m = re.search(r"(https?://(?:localhost|127\.0\.0\.1)(?::\d+)?[A-Za-z0-9_\-/\.]*)", docs)
base = m.group(1).rstrip("/") if m else ""
paths = [p for p in dict.fromkeys(re.findall(r"(/[a-zA-Z0-9_\-/]{2,40})", docs)) if "{" not in p][:6]
keys = re.findall(r"(?:api[-_]?key|token|secret|key)\s*[:=：]\s*[\"']?([A-Za-z0-9_\-]{6,40})", docs, re.I)
STOP = "查询 今天 明日 天气 数据 接口 返回 任务 城市 所有 全部 相关 统计 列出 给出 需要 通过 调用 结果 数量 类型 名称 今日 本地 获取 搜索 帮我 请问".split()
cities = []
for src in (TASK_TEXT, docs):
    cleaned = src
    for w in STOP:
        cleaned = cleaned.replace(w, " ")
    for c in re.findall(r"[\u4e00-\u9fa5]{2,3}", cleaned):
        if c not in cities:
            cities.append(c)
cities = cities[:6]

def fetch(url, headers):
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            return e.code, ""
    except Exception:
        return -1, ""

def auths():
    k = keys[0] if keys else "token"
    yield "none", {}
    yield "bearer", {"Authorization": "Bearer " + k}
    yield "x-api-key", {"X-API-Key": k}

hints = set()
best = None
for path in paths or ["/"]:
    for auth_name, headers in auths():
        for param in ("", "city", "location", "cityName", "q", "name"):
            for city in ([""] if not param else cities or [""]):
                url = base + path
                if param:
                    url += "?" + param + "=" + urllib.parse.quote(city)
                status, text = fetch(url, headers)
                if status in (400, 401, 403) and text:
                    hm = re.search(r"(Missing required parameter[^\"]{0,60}|Authentication failed[^\"]{0,60}|Expected format[^\"]{0,60})", text)
                    if hm:
                        hints.add(hm.group(1)[:90])
                if status == 200 and text.strip():
                    recs, total = [], 0
                    try:
                        data = json.loads(text)
                        if isinstance(data, dict):
                            for v in data.values():
                                if isinstance(v, list):
                                    recs = v
                                    break
                            total = data.get("total") or data.get("total_count") or data.get("count") or len(recs)
                        elif isinstance(data, list):
                            recs, total = data, len(data)
                    except Exception:
                        continue
                    if recs or (isinstance(total, int) and total > 0):
                        print("__API status=OK base=%s path=%s auth=%s param=%s city=%s records=%d total=%s" % (base, path, auth_name, param, city, len(recs), total))
                        if recs and isinstance(recs[0], dict):
                            print("__API_KEYS %s" % json.dumps(sorted(recs[0].keys()), ensure_ascii=False))
                        # 直接合成答案（零 LLM）：city + total_count + 类型分布 + 世界遗产计数
                        ans = {}
                        if city:
                            ans["city"] = city
                        try:
                            ans["total_count"] = int(total) if total else len(recs)
                        except Exception:
                            ans["total_count"] = len(recs)
                        if recs and isinstance(recs[0], dict):
                            tk = next((k for k in recs[0] if str(k).lower() in ("type", "category", "类型", "level")), None)
                            if tk:
                                dist = {}
                                for rr in recs:
                                    tv = str(rr.get(tk, ""))
                                    if tv:
                                        dist[tv] = dist.get(tv, 0) + 1
                                ans["types"] = dist
                            wh = sum(1 for rr in recs if any(("世界" in str(v) or "遗产" in str(v)) for v in rr.values()))
                            ans["world_heritage_count"] = wh
                        print("__ANSWER %s" % json.dumps(ans, ensure_ascii=False))
                        if recs:
                            print("__ANSWER_CANDIDATE %s" % json.dumps(recs[:60], ensure_ascii=False))
                        best = True
                        break
            if best: break
        if best: break
    if best: break
for h in list(hints)[:4]:
    print("__API_HINT %s" % h)
if not best:
    print("__API status=FAIL base=%s paths=%d keys=%d cities=%s" % (base, len(paths), len(keys), ",".join(cities)))
'''

# LLM prompt（严格单行协议）
LLM_PROMPT = (
    "你在生存塔防比赛中用沙盒完成探索任务，每回合只能给一条指令。\n"
    "任务原文：\n{task}\n"
    "已读取的任务文档：\n{desc}\n"
    "沙盒证据（命令+输出）：\n{evidence}\n"
    "{sop}"
    "规则：只输出一行，二选一：\n"
    "CMD: <单行shell命令>   （还需要探索时）\n"
    "ANSWER: <JSON答案>     （能作答时，字段完整，不要多余文字）\n"
    "禁止输出 cat 已读过的文档；命令不超过 300 字符；无把握就给最佳猜测 ANSWER。"
)

REFINE_PROMPT = (
    "你在生存塔防比赛中完成探索任务。只输出一行：CMD: <命令> 或 ANSWER: <JSON答案>。\n"
    "任务原文：\n{task}\n"
    "上次提交被判错：{last_answer}\n错误反馈：{error}\n"
    "沙盒证据：\n{evidence}\n"
    "请修正后输出完整 ANSWER。"
)

MAX_LLM_LOOPS = 4       # 振荡降级阈值
MAX_CMD_FAILS = 4       # 连续命令失败上限
EVIDENCE_LIMIT = 30000
SUBMIT_RETRY_CAP = 2    # submitAnswer 被拒后的重发预算（参考实现：拒收常为瞬时性，重发可挽回）
TOKEN_HINT = re.compile(r"\bTOKEN\s*[:：]?\s*([0-9a-fA-F]{8,})")  # token 类任务直提（hex≥8）
REFUSAL_HINT = re.compile(
    r"(无法(直接)?(作答|回答|获取|读取|完成)|cannot (answer|determine)|unable to|"
    r"no such file or directory|not found|permission denied|command not found|"
    r"无法确定|信息不足|缺少(必要)?信息)"
)


@dataclass
class TaskSession:
    """Task Memory：单任务会话全部上下文。"""

    task_text: str = ""
    stage: str = "LOCATE"          # LOCATE / WS_PROBE / WS_FIX / API_HARVEST / LLM / SUBMITTED
    task_dir: str = ""
    task_desc: str = ""
    task_kind: str = ""            # ws / api / llm
    evidence: list = field(default_factory=list)   # [(cmd, result)]
    pending_cmd: str | None = None
    pending_llm_cmd: str | None = None
    llm_pending: bool = False
    best_answer: dict | None = None
    submitted: bool = False
    need_refine: bool = False
    llm_loops: int = 0
    cmd_fails: int = 0
    soft_fails: int = 0
    ws_fix_count: int = 0
    quiet_rounds: int = 0
    auth_retried: bool = False
    param_fixes: int = 0
    tried_params: set = field(default_factory=set)
    timeout_rounds: int = 0  # 由 brain 从 PlayerTask.timeoutRounds 注入
    error_log: list = field(default_factory=list)  # 累积去重的错误信息（送 LLM 推理识别）
    submit_retries: int = 0        # submitAnswer 已重发次数
    submit_rejected: bool = False  # 上回合 submit 被判拒（由 brain 注入）

    def reset(self) -> None:
        self.__init__()

    def add_error(self, msg: str) -> None:
        msg = (msg or "").strip()[:200]
        if msg and msg not in self.error_log:
            self.error_log.append(msg)
            if len(self.error_log) > 8:
                self.error_log.pop(0)


@dataclass
class PlannerOutput:
    prompt: str = ""
    execute_cmd: str = ""
    submit: dict | None = None


def _clip(text: str, n: int) -> str:
    return (text or "")[:n]


def _extract_json(text: str) -> dict | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _quotes_open(cmd: str) -> bool:
    """命令末尾是否停在未闭合的引号里（shell 引号状态机，参考实现同款）。
    正确处理 `'` 在双引号内、`"` 在单引号内都是字面量的语义。"""
    in_sq = in_dq = False
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if c == "\\" and not in_sq:  # 单引号内反斜杠不转义
            i += 2
            continue
        if c == "'" and not in_dq:
            in_sq = not in_sq
        elif c == '"' and not in_sq:
            in_dq = not in_dq
        i += 1
    return in_sq or in_dq


def _is_answer_like(text: str) -> bool:
    """这段文本能否当答案提交？（参考实现 _not_an_answer：所有出口统一过此闸）
    拒绝：空壳/纯符号、shell 报错、散文拒答。"""
    t = (text or "").strip()
    if not t or not re.search(r"[0-9A-Za-z\u4e00-\u9fa5]", t):
        return False  # 空壳/纯符号（如 "/"）不是答案
    if REFUSAL_HINT.search(t):
        return False  # shell 报错/拒答不是答案
    return True


class TaskPlanner:
    def __init__(self) -> None:
        self.api_facts: dict[str, str] = {}  # 跨任务复用：认证方式/参数名等

    # ---- 主入口 ----
    def work(self, turn: Turn, session: TaskSession) -> PlannerOutput:
        out = PlannerOutput()
        if not turn.phase_task:
            session.reset()
            return out
        if turn.phase_task != session.task_text:  # 新任务 → 重置会话
            session.reset()
            session.task_text = turn.phase_task

        # 1. 回收异步结果
        if session.pending_cmd is not None:
            result = turn.last_cmd_result or ""
            session.evidence.append((session.pending_cmd, _clip(result, 3000)))
            self._on_cmd_result(session, session.pending_cmd, result)
            session.pending_cmd = None
        if session.llm_pending:
            self._on_llm_result(session, turn.llm_resp or "")
            session.llm_pending = False
        if any(code == 2 for code, _ in turn.errors):
            session.need_refine = True
            session.submitted = False
        # submit 被判拒 → 清 submitted 允许重发（参考实现：拒收常瞬时性，重发可挽回；限 2 次）
        if session.submit_rejected:
            session.submit_rejected = False
            if session.submit_retries < SUBMIT_RETRY_CAP:
                session.submit_retries += 1
                session.submitted = False

        # 2. LLM 刚给的命令优先下发
        if session.pending_llm_cmd is not None:
            cmd = session.pending_llm_cmd
            session.pending_llm_cmd = None
            out.execute_cmd = cmd
            session.pending_cmd = cmd
            return out

        # 3. 有可提交答案（TOKEN / LLM ANSWER / 振荡降级保底）
        if session.best_answer and not session.submitted:
            out.submit = self._submit(session)
            return out

        # 4. 认证/参数纠错：确定性重试优先于 LLM（实战：Bearer 头 + location 参数）
        retry = self._auth_retry_cmd(session) or self._param_fix_cmd(session)
        if retry is not None:
            out.execute_cmd = retry
            session.pending_cmd = retry
            return out

        # 4b. 超时预算：任务 timeout（实战=15 回合）逼近 → 最后一次向 LLM 要最佳猜测答案，否则停止浪费
        if session.timeout_rounds > 0:
            deadline = session.started_round + session.timeout_rounds
            if turn.round_no >= deadline - 2:
                if session.best_answer and not session.submitted:
                    out.submit = self._submit(session)
                elif not session.llm_pending and session.quiet_rounds >= 1:
                    # 最后一搏：把已收集证据+错误喂给 LLM，要求直接给最佳猜测 ANSWER
                    out.prompt = (
                        "时间即将耗尽，必须立刻作答。只输出一行 ANSWER: <JSON答案>。\n"
                        f"任务原文：{_clip(session.task_text, 800)}\n"
                        f"沙盒证据：\n{_history(session)}\n"
                        f"遇到的错误：{'; '.join(session.error_log[-4:])}\n"
                        "即使信息不全，也请给出字段尽量完整的最佳猜测 JSON。"
                    )
                    session.llm_pending = True
                    session.llm_loops += 1
                return out

        # 5. 阶段推进
        session.quiet_rounds += 1
        if session.stage == "LOCATE":
            out.execute_cmd = LOCATE_CMD
            session.pending_cmd = LOCATE_CMD
        elif session.stage == "WS_PROBE":
            cmd = WS_PROBE_CMD.replace("{dir}", session.task_dir or ".")
            out.execute_cmd = cmd
            session.pending_cmd = cmd
        elif session.stage == "WS_FIX":
            cmd = self._ws_fix_cmd(session)
            if cmd is None:
                session.stage = "LLM"
            else:
                out.execute_cmd = cmd
                session.pending_cmd = cmd
        elif session.stage == "API_HARVEST":
            cmd = self._harvest_cmd(session)
            out.execute_cmd = cmd
            session.pending_cmd = cmd
        elif session.stage == "LLM":
            if session.llm_loops >= MAX_LLM_LOOPS:
                return out  # 振荡降级：无保底答案，放弃本任务（PioneerFSM 会带我回家）
            self._llm_prompt(turn, session, out, refine=session.need_refine)
        elif session.stage == "SUBMITTED":
            if session.need_refine:
                session.stage = "LLM"
                session.llm_loops = 0
                self._llm_prompt(turn, session, out, refine=True)
            elif session.quiet_rounds >= MAX_LLM_LOOPS:
                # 部分正确等待中：继续让 LLM 提升通过率
                session.stage = "LLM"
                session.llm_loops = 1
                self._llm_prompt(turn, session, out, refine=False)
        return out

    # ---- 命令结果处理 ----
    def _on_cmd_result(self, session: TaskSession, cmd: str, result: str) -> None:
        session.quiet_rounds = 0
        if cmd == LOCATE_CMD:
            self._classify(session, result)
            return
        if "[exitCode:0]" in result:
            session.cmd_fails = 0
        else:
            session.cmd_fails += 1
            # 累积错误信息送 LLM 推理识别（用户要求#3）
            m = re.search(r"\[exitCode:(-?\d+)\]\s*(.{0,160})", result, re.S)
            session.add_error(f"exit{m.group(1) if m else '?'}: {(m.group(2) if m else result)[:160]}")
        if "[FAIL]" in result:
            session.soft_fails += 1
        # 提取服务端语义错误（认证/参数）入 error_log，供 LLM 兜底识别
        for em in re.finditer(
            r"(Authentication failed[^\"]{0,80}|Missing required parameter[^\"]{0,40}"
            r"|Missing 'Authorization'[^\"]{0,60}|Expected format[^\"]{0,60}"
            r"|No such file[^\"]{0,40}|not found[^\"]{0,40})",
            result,
        ):
            session.add_error(em.group(1).strip())
        # token 直提：任务提到 token，或 ./check 通过（[ OK ]/全部通过）且输出含 hex TOKEN
        token = TOKEN_HINT.search(result)
        check_passed = bool(re.search(r"(\[ OK \]|全部通过|all .{0,10}pass)", result, re.I))
        task_mentions_token = "token" in (session.task_text or "").lower()
        if token and (task_mentions_token or check_passed) and session.best_answer is None:
            session.best_answer = {"token": token.group(1)}
        for line in result.splitlines():
            if line.startswith("__API "):
                session.evidence.append(("__api_fact__", line[:300]))
                m = re.search(r"auth=(\S+)", line)
                if m and m.group(1) != "none":
                    self.api_facts["auth"] = m.group(1)
            elif line.startswith("__ANSWER "):
                ans = _extract_json(line[len("__ANSWER "):])
                if ans and session.best_answer is None:
                    session.best_answer = ans  # HARVEST 直接合成答案 → 零 LLM 提交
            elif line.startswith("__ANSWER_CANDIDATE "):
                session.evidence.append(("__candidate__", line[:800]))
        # WS 流程推进：probe 完 → 尝试确定性修复
        if session.stage == "WS_PROBE":
            session.stage = "WS_FIX"
        elif session.stage == "API_HARVEST":
            # 已直接合成答案则保持阶段（work() 第3步会提交），否则交 LLM 组答
            if session.best_answer is None:
                session.stage = "LLM"
        if session.cmd_fails >= MAX_CMD_FAILS:
            session.stage = "LLM"

    def _classify(self, session: TaskSession, result: str) -> None:
        m = re.search(r"__DIR:(\S+)", result)
        if m:
            session.task_dir = m.group(1)
        body = result[:8000]
        session.task_desc = _clip(body, 2000)
        docs = re.findall(r"__DOC:(\S+)", result)
        if ("ws_" in body and "./check" in body) or ("ws_" in session.task_text and "check" in session.task_text):
            session.task_kind = "ws"
            session.stage = "WS_PROBE"
        elif "localhost" in body or "http://" in body or "https://" in body:
            session.task_kind = "api"
            session.stage = "API_HARVEST"
        else:
            session.task_kind = "llm"
            session.stage = "LLM"  # 定位失败/其他 → LLM 兜底

    def _auth_retry_cmd(self, session: TaskSession) -> str | None:
        """服务端报 Missing 'Authorization' header → 用 Bearer 重发上次的请求（实战教训）。"""
        if session.auth_retried:
            return None
        # 证据需含命令本身（密钥与 URL 在命令里，错误在输出里）
        text = "\n".join(c + "\n" + r for c, r in session.evidence[-3:])
        if "Missing 'Authorization' header" not in text and "Expected format" not in text:
            return None
        key = None
        for pat in (
            r"(?:api[-_]?key|token|secret|key)\s*[:=：]\s*[\"']?([A-Za-z0-9_\-]{6,40})",
            r"X-API-Key:\s*([A-Za-z0-9_\-]{6,40})",
            r"Bearer\s+([A-Za-z0-9_\-]{6,40})",
        ):
            m = re.search(pat, session.task_desc + "\n" + text, re.I)
            if m:
                key = m.group(1)
                break
        if key is None:
            return None
        urls = re.findall(r'"(https?://[^"]+)"', text)
        if not urls:
            return None
        session.auth_retried = True
        return f'curl -s -H "Authorization: Bearer {key}" "{urls[-1]}"'

    # ---- 工程修复类 ----
    def _ws_fix_cmd(self, session: TaskSession) -> str | None:
        if session.ws_fix_count >= 2:
            return None  # 超过 2 次修复尝试 → 交 LLM
        fixes = self._spec_fixes(session)
        if not fixes:
            return None
        session.ws_fix_count += 1
        body = " && ".join(fixes + ["./check"])
        return f'cd {session.task_dir or "."} && {body}'

    def _spec_fixes(self, session: TaskSession) -> list[str]:
        """从 spec/check 输出解析确定性修复指令（mkdir/chmod/sed 行替换）。"""
        text = session.task_desc + "\n" + "\n".join(r for _, r in session.evidence[-3:])
        fixes: list[str] = []
        for m in re.finditer(r"\[FAIL\]\s*DIR\s*([/\w.\-]+)[^\d]*(\d{3})", text):
            fixes.append(f'mkdir -p "{m.group(1)}" && chmod {m.group(2)} "{m.group(1)}"')
        for m in re.finditer(r"\[FAIL\]\s*LINE\s*([\w.\-]+):(\d+)\s*期望\s*([^\s]+)", text):
            fixes.append(f"sed -i '{m.group(2)}s/.*/{m.group(3)}/' {m.group(1)}")
        for m in re.finditer(r"(?:目录|文件)\s*[：: ]*\s*([/\w.\-]+)[^\n]*?(?:权限|mode)\s*[:： ]?\s*(\d{3})", text):
            fixes.append(f'mkdir -p "{m.group(1)}" && chmod {m.group(2)} "{m.group(1)}"')
        # 实战格式："- logs/alpha/ 必须存在，权限为 755"
        for m in re.finditer(r"-\s*([/\w.\-]+/)\s*必须存在[，,]?\s*权限为\s*(\d{3})", text):
            fixes.append(f'mkdir -p "{m.group(1)}" && chmod {m.group(2)} "{m.group(1)}"')
        for m in re.finditer(r"第\s*(\d+)\s*行[：:]\s*`([^`]+)`", text):
            target = re.search(r"([\w.\-]+\.(?:conf|cfg|ini|txt|yaml|yml|json))", text)
            fname = target.group(1) if target else "config.conf"
            fixes.append(f"sed -i '{m.group(1)}s/.*/{m.group(2)}/' {fname}")
        for m in re.finditer(r"第\s*(\d+)\s*行[^\n]*?(?:改为|应为|->)\s*[`'\"]?([^\n`'\"]+)", text):
            target = re.search(r"([\w.\-]+\.(?:conf|cfg|ini|txt|yaml|yml|json))", text)
            fname = target.group(1) if target else "config.conf"
            fixes.append(f"sed -i '{m.group(1)}s/.*/{m.group(2).strip()}/' {fname}")
        return list(dict.fromkeys(fixes))

    # ---- API 类 ----
    def _harvest_cmd(self, session: TaskSession) -> str:
        task_b64 = base64.b64encode(session.task_text.encode("utf-8")).decode()
        script = HARVEST_PY.replace("__TASK_B64__", task_b64)
        encoded = base64.b64encode(script.encode()).decode()
        return (
            f"cd {session.task_dir or '.'} 2>/dev/null; "
            f"python3 -c \"import base64;exec(base64.b64decode('{encoded}').decode())\" "
            f"|| python -c \"import base64;exec(base64.b64decode('{encoded}').decode())\""
        )

    def _param_fix_cmd(self, session: TaskSession) -> str | None:
        """'Missing required parameter: X' → 用任务原文里的城市名确定性重试（不耗 LLM）。"""
        if session.param_fixes >= 2:
            return None
        text = "\n".join(c + "\n" + r for c, r in session.evidence[-2:])
        m = re.search(r"Missing required parameter[:\s'\"]*([A-Za-z_]\w{0,20})", text)
        if not m:
            return None
        param = m.group(1)
        if param in session.tried_params:
            return None
        city = self._extract_city(session.task_text, session.task_desc)
        if not city:
            return None
        url = None
        urls = re.findall(r'"(https?://[^"]+)"', text)
        if urls:
            url = urls[-1].split("?")[0]
        if url is None:
            m2 = re.search(r"base=(\S+)", text)
            if m2:
                url = m2.group(1)
        if url is None:
            return None
        key = None
        for pat in (
            r"Bearer\s+([A-Za-z0-9_\-]{6,40})",
            r"(?:api[-_]?key|token|secret|key)\s*[:=：]\s*[\"']?([A-Za-z0-9_\-]{6,40})",
        ):
            km = re.search(pat, session.task_desc + "\n" + text, re.I)
            if km:
                key = km.group(1)
                break
        session.tried_params.add(param)
        session.param_fixes += 1
        auth = f'-H "Authorization: Bearer {key}" ' if key else ""
        return f'curl -s {auth}"{url}?{param}={city}"'

    @staticmethod
    def _extract_city(task_text: str, extra: str = "") -> str | None:
        stop = ("查询", "今天", "明日", "天气", "数据", "接口", "返回", "任务",
                "城市", "所有", "全部", "相关", "统计", "列出", "给出", "需要",
                "通过", "调用", "结果", "数量", "类型", "名称", "今日", "本地",
                "获取", "搜索", "帮我", "请问", "请阅读", "阅读", "信息", "作业",
                "文化遗产", "遗产", "请", "读取", "查看", "完成", "回答")
        text = (task_text or "") + " " + (extra or "")
        # 去掉指针文件名 task_1_beijing.md → 提取 pinyin 城市名（beijing/nanjing/chengdu…）
        for w in stop:
            text = text.replace(w, " ")
        # 1) 文件名里的 pinyin 城市 → 中文
        pin = re.search(r"task_\d+_([a-z]{3,12})", text + " " + (extra or ""))
        if pin:
            PY = {"beijing": "北京", "nanjing": "南京", "chengdu": "成都",
                  "shanghai": "上海", "guangzhou": "广州", "xian": "西安",
                  "hangzhou": "杭州", "suzhou": "苏州", "luoyang": "洛阳"}
            city = PY.get(pin.group(1).lower())
            if city:
                return city
        for cand in re.findall(r"[\u4e00-\u9fa5]{2,3}", text):
            return cand
        return None

    # ---- LLM 循环 ----
    def _llm_prompt(self, turn: Turn, session: TaskSession, out: PlannerOutput, *, refine: bool) -> None:
        evidence = "\n".join(f"$ {c}\n{r}" for c, r in session.evidence[-6:])[:4000]
        # 累积错误清单（用户要求#3：把错误信息都送给 LLM 推理识别）
        errors = "\n".join(f"- {e}" for e in session.error_log[-8:])
        if refine:
            out.prompt = (
                REFINE_PROMPT.replace("{task}", _clip(session.task_text, 1500))
                .replace("{last_answer}", json.dumps(session.best_answer or {}, ensure_ascii=False))
                .replace("{error}", ";".join(d for c, d in turn.errors if c == 2))
                .replace("{evidence}", evidence)
            )
            session.need_refine = False
        else:
            sop = ""
            if self.api_facts:
                sop = "已知 API 经验：" + json.dumps(self.api_facts, ensure_ascii=False) + "\n"
            if errors:
                sop += "已遇到的错误（请据此推理修正，勿重复同样错误）：\n" + errors + "\n"
            out.prompt = (
                LLM_PROMPT.replace("{task}", _clip(session.task_text, 1500))
                .replace("{desc}", _clip(session.task_desc, 2000))
                .replace("{evidence}", evidence)
                .replace("{sop}", sop)
            )
        session.llm_pending = True
        session.llm_loops += 1

    def _on_llm_result(self, session: TaskSession, text: str) -> None:
        session.quiet_rounds = 0
        text = (text or "").strip()
        if not text:
            return
        # 兼容 LLM 输出前后缀说明/多行：在整段文本中定位 ANSWER:/CMD:（实战 LLM 常带解释文字）
        ans_idx = text.find("ANSWER:")
        cmd_idx = text.find("CMD:")
        if ans_idx >= 0 and (cmd_idx < 0 or ans_idx < cmd_idx):
            body = text[ans_idx + 7:].strip()
            if _is_answer_like(body):
                answer = _extract_json(body)
                if answer:
                    session.best_answer = answer
                    return
        if cmd_idx >= 0:
            cmd = text[cmd_idx + 4:].strip().splitlines()[0].strip()
            # 命令消毒：引号不闭合（状态机判定）会在沙盒 bash 里爆炸（参考实现 _quotes_open）
            if cmd and not _quotes_open(cmd) and not cmd.startswith("cat "):
                session.pending_llm_cmd = cmd
                return
        # 兜底：LLM 直接给了 JSON 对象 → 当作答案
        answer = _extract_json(text)
        if answer and not session.best_answer:
            session.best_answer = answer

    # ---- 提交 ----
    def _submit(self, session: TaskSession) -> dict:
        session.submitted = True
        session.stage = "SUBMITTED"
        return submit_answer_command(json.dumps(session.best_answer, ensure_ascii=False))
