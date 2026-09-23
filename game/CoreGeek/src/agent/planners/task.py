"""L3 TaskPlanner：自进化任务（LLM 驱动 + 健壮沙箱命令 + 家族经验）。

重构（2026-09-23 issue IKI8DZ，用户要求）：
1. **移除 HARVEST 硬编码**：不再内嵌/执行 base64 API 收割脚本，不再产出 `__ANSWER`。
   API 任务与工程任务一样交给 LLM——LLM 能直接给答案就给答案；需要探测就返回一条
   curl 命令（`{"cmd": "...", "answer": "", "isFinished": false}`），沙箱执行后把结果
   喂回下一轮，由 LLM 继续思考/收敛（"curl 不通就给指令，curl 通了就直接给答案"）。
2. **每次 LLM 交互都带上下文与历史经验**：任务原文 + transcript（沙箱命令累积执行记录，
   按 30k 规则截断：前 12k + 后 18k）+ 同族 SOP/经验笔记 + 跨任务 API 经验 + 上次判错原因
   + 强制提交提示。transcript 是核心上下文载体。
3. **task_type 分类**（api / engineering / general）：按 transcript 关键词判定，关键词表
   适度扩充（避免误判），分类结果按族固化 SOP 与经验（跨任务/跨城市复用）。
4. **保留健壮命令**：单条探索（定位任务文件 + 读同目录文档 + 列目录权限）、工程 `./check`
   探测（去 CRLF + chmod）、`[FAIL] DIR/LINE` 确定性修复、`TOKEN:` 自动提取
   （均在 `config.HARDCODED_ASSIST` 开关下）。
5. **家族经验学习**（`learn_family_notes`）：从 4xx 响应学"认证方式 / 必须传的参数 /
   不接受的参数"，从 200 成功命令学实际认证头/参数名/路径；注入同族后续任务 prompt。
6. **分页完整性提示**：API 任务从 transcript 解析已收割记录，去重后与 total_count 比对，
   不完整则在 prompt 里提示 LLM 用更大 offset 继续取（修"只取第一页"→ `9 != 7` 根因）。

阶段：EXPLORE_FILES →（ENGINEER_PROBE → ENGINEER_FIX?）→ LLM_LOOP
      → WAIT_CMD_RESULT / WAIT_LLM → SUBMIT_ANSWER → COMPLETED
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..protocol import Turn, distance, submit_answer_command
from .. import config

# ---- 阶段 ----
ST_EXPLORE = "EXPLORE_FILES"
ST_PROBE = "ENGINEER_PROBE"
ST_FIX = "ENGINEER_FIX"
ST_LLM = "LLM_LOOP"
ST_WAIT_CMD = "WAIT_CMD_RESULT"
ST_WAIT_LLM = "WAIT_LLM"
ST_SUBMIT = "SUBMIT_ANSWER"
ST_DONE = "COMPLETED"
# 兼容旧名（外部引用）
ST_FIND = ST_EXPLORE
ST_READ = ST_EXPLORE

MAX_NON_JSON = 3       # 连续非 JSON 上限 → 强制结束
MAX_LLM_LOOPS = 8      # LLM 循环上限默认值（实际按任务 timeoutRounds 收紧，见 _task_timeout）
FORCE_SUBMIT_CMDS = 8  # 命令预算默认值（实际按 timeout 动态，见 max_cmds）
FORCE_ANSWER_MARGIN = 2  # 距任务超时 ≤ 此回合 → 强制"只给答案"模式
EXPLORE_LIMIT = 20000  # 探索输出保留字符数（放开：需容纳 API_DOCS 全文/所有 md·txt/密钥）

# transcript 长度控制（issue IKI8DZ §2.3）：单条结果保留末尾 16000；
# 总长 ≤30000 全留，否则前 12000 + 后 18000，中间以省略标记替代。
TRANSCRIPT_RESULT_MAX = 16000
TRANSCRIPT_MAX = 30000
TRANSCRIPT_HEAD = 12000
TRANSCRIPT_TAIL = 18000
TRANSCRIPT_OMIT = "\n...[middle transcript omitted]...\n"

_FILE_NAME = re.compile(r"[A-Za-z0-9_\-/]+\.(?:md|txt)", re.I)
_JSON_BLOCK = re.compile(r"\{.*\}", re.S)
# 行首锚定（re.M）：只认 check 脚本输出里的独立行 `TOKEN: xxx`，
# 不匹配任务书示例里的 `"token": "xxx"`（issue#26 误提交占位符的根因）。
_TOKEN = re.compile(r"^\s*TOKEN\s*[:：]\s*([A-Za-z0-9_\-]{6,})\s*$", re.M)
_TOKEN_PLACEHOLDERS = {"xxx", "xxxx", "token", "your_token", "your-token", "todo", "none"}
# 命令输出里的 `FINAL_ANSWER: {json}`（LLM 跑脚本后自报答案，answer_from_result）
_FINAL_ANSWER = re.compile(r"FINAL_ANSWER\s*[:：]\s*(\{.*\})", re.S)
_WS_DIR = re.compile(r"__WS:(\S+)")
_DIR = re.compile(r"__DIR:(\S*)")
_FILE = re.compile(r"__FILE:(\S*)")
# 工程 check 的 FAIL 行
_FAIL_DIR = re.compile(r"\[FAIL\]\s*DIR\s+(\S+)\s*[→>\-]*\s*期望\s+exists\s*,?\s*(\d{3})")
_FAIL_LINE = re.compile(r"\[FAIL\]\s*LINE\s+(\S+?):(\d+)\s*[→>\-]*\s*期望\s+(.+?)\s*[，,]\s*实际")
_SPEC_LINE = re.compile(r"第\s*(\d+)\s*行[：:]\s*[`\"']?([^`\"'\n]+)[`\"']?")
_SPEC_DIR = re.compile(r"[-*]\s*(\S+/?)\s*必须存在[，,]?\s*权限为?\s*(\d{3})")
# 确定性回答（local_answer，零 LLM）：显式答案字段 + 简单算术
_EXPLICIT_ANSWER = re.compile(r"(?:答案|answer)\s*(?:是|为)?\s*[:：=]\s*([^\s，,。;；]{1,80})", re.I)
_ARITH = re.compile(r"(?:计算|求|compute|calculate)\s*[:：]?\s*([0-9(][0-9+\-*/%().\s]{0,80})")
_ANSWER_PLACEHOLDERS = {"xxx", "xx", "todo", "none", "null", "n/a", "your_answer", "占位"}

# ---- task_type 分类关键词（issue IKI8DZ §4.2/§4.3，适度扩充、强信号优先）----
# 只做"够用且不易误判"的关键词，避免把普通文本误分类。
_ENG_STRONG = ("./check", "[fail]", "[pass]", "spec.md", "ws_")
_ENG_WEAK = ("chmod ", "mkdir ", "权限")
_API_STRONG = ("localhost:", "127.0.0.1", "/api/", "api_docs", "x-api-key", "bearer ")
# 弱信号（半分）：覆盖 §4.3 盲区（路径不含 /api/、用 http:// 等），但不过量以免误判
_API_WEAK = ("curl ", "pagination", "total_count", "authorization:", "http://", "https://", "heritage")

# 家族经验学习：从 4xx 响应体提取可复用事实（issue IKI8DZ §5.2）
_NOTE_PATTERNS = (
    # 真实报文：`Expected format: 'Authorization: Bearer <api_key>'`（引号曾导致漏学）
    (re.compile(r"expected format:\s*'?\"?\s*authorization:\s*bearer", re.I),
     "认证方式：用 Authorization: Bearer <key>"),
    (re.compile(r"missing required parameter:\s*'?\"?([A-Za-z0-9_\-]+)", re.I),
     "必须传参数：{0}"),
    (re.compile(r"unknown parameter:\s*'?\"?([A-Za-z0-9_\-]+)", re.I),
     "不接受参数：{0}（不要传）"),
)


def _extract_file_names(text: str) -> list[str]:
    out = []
    for m in _FILE_NAME.findall(text or ""):
        name = m.rsplit("/", 1)[-1]      # 只要文件名
        if name not in out:
            out.append(name)
    return out


# 兼容旧名
_extract_md_names = _extract_file_names


def _parse_llm_json(text: str) -> dict | None:
    """JSON 解析容忍：先直接 loads，失败再用正则提 {...} 块。"""
    t = (text or "").strip()
    if not t:
        return None
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass
    for blk in _JSON_BLOCK.findall(t):
        try:
            obj = json.loads(blk)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _classify(text: str) -> str:
    """按 transcript 关键词判定 task_type：engineering / api / general。

    强信号优先，弱信号半分；平手时 engineering 优先（工程类更依赖确定性路径）。
    只做够用且不易误判的判定，避免误分类。
    """
    low = (text or "").lower()
    eng = sum(1 for s in _ENG_STRONG if s in low) + 0.5 * sum(1 for s in _ENG_WEAK if s in low)
    api = sum(1 for s in _API_STRONG if s in low) + 0.5 * sum(1 for s in _API_WEAK if s in low)
    if eng <= 0 and api <= 0:
        return "general"
    return "engineering" if eng >= api else "api"


def _safe_eval(expr: str) -> Any:
    """白名单算术求值（local_answer）：只允许常量与 + - * / // % ** 一元运算。"""
    try:
        node = ast.parse(expr, mode="eval")
    except (SyntaxError, ValueError):
        return None
    allowed = (
        ast.Expression, ast.Constant, ast.BinOp, ast.UnaryOp,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
        ast.USub, ast.UAdd,
    )
    for n in ast.walk(node):
        if not isinstance(n, allowed):
            return None
    try:
        return eval(compile(node, "<local_answer>", "eval"), {"__builtins__": {}}, {})
    except Exception:
        return None


@dataclass
class TaskSession:
    stage: str = ST_EXPLORE
    task_text: str = ""
    task_key: str = ""                                   # 任务指纹（展示用）
    task_type: str = ""                                  # 任务类型：api/engineering/general（SOP 按类型固化）
    target_name: str = "task_*.md"                       # 待定位的任务文件名
    task_dir: str = ""                                   # 工作目录（工程类=ws 目录）
    explore_output: str = ""                             # 健壮探索的完整输出
    explore_sent: bool = False
    engineer: bool = False
    probe_sent: bool = False
    crlf_fixed: bool = False
    city: str = ""                                       # API 任务城市（prompt 线索）
    fix_rounds: int = 0                                  # 工程确定性修复轮数（防死循环）
    pending_fix: str | None = None                       # 待下发的确定性修复命令
    files: dict = field(default_factory=dict)            # 文件名 → 内容（兼容旧引用）
    pending_cmd: str | None = None
    cmd_history: list = field(default_factory=list)      # [(cmd, result)]
    transcript: list = field(default_factory=list)       # ["COMMAND: ...\nRESULT: ..."]（LLM 上下文核心）
    llm_pending: bool = False
    last_llm_raw: str = ""
    non_json: int = 0
    llm_loops: int = 0
    answer: Any = None
    submitted: bool = False
    need_refine: bool = False
    max_loops: int = MAX_LLM_LOOPS                          # 本任务 LLM 循环上限（按 timeout 收紧）
    cmd_count: int = 0                                      # 已下发命令数
    last_error: str = ""                                    # 上次提交被判错的原因（重试时喂 LLM）
    # ---- 超时模型（按剩余回合驱动）----
    accept_round: int = 0                                   # 接取任务的回合
    timeout_rounds: int = 0                                 # 任务超时回合数（平台给）
    max_cmds: int = FORCE_SUBMIT_CMDS                       # 命令预算（按 timeout 动态）
    force_sent: bool = False                                # 是否已发过"强制答案"prompt
    force_answer: bool = False                              # 强制只给答案模式（拒绝新命令）
    local_done: bool = False                                # local_answer 已判定（零 LLM 直答）

    def reset(self) -> None:
        self.__init__()


@dataclass
class PlannerOutput:
    prompt: str = ""
    execute_cmd: str = ""
    submit: dict | None = None


class TaskPlanner:
    def __init__(self) -> None:
        # SOP 自进化（IKHYTW §12.6）：**按任务类型（api/engineering/general）固化**，
        # 而非按任务文件名——这样"北京 API 任务"的经验能复用到"南京 API 任务"，避免重复横跳。
        self.sop: dict[str, str] = {}          # task_type → SOP 文本（跨任务复用）
        self.api_facts: dict[str, str] = {}    # 跨任务 API 经验（认证/参数名/path，从 200 成功命令学习）
        self.notes: dict[str, list[str]] = {}  # task_type → 家族经验笔记（4xx 失败教训等）
        self.completed: int = 0                # 已完成任务数

    # ---- 主入口 ----
    def work(self, turn: Turn, session: TaskSession) -> PlannerOutput:
        out = PlannerOutput()
        if not turn.phase_task:
            session.reset()
            return out
        if turn.phase_task != session.task_text:      # 新任务 → 重置
            session.reset()
            session.task_text = turn.phase_task
            session.task_key = self._task_key(turn.phase_task)
            names = _extract_file_names(turn.phase_task)
            session.target_name = names[0] if names else "task_*.md"
            # 超时模型（按剩余回合驱动）：循环/命令预算 = max(2, (timeout-4)//2)
            session.accept_round = turn.round_no
            session.timeout_rounds = self._task_timeout(turn)
            if session.timeout_rounds > 0:
                budget = max(2, (session.timeout_rounds - 4) // 2)
                session.max_loops = min(MAX_LLM_LOOPS, budget)
                session.max_cmds = min(MAX_LLM_LOOPS, budget)
            session.stage = ST_EXPLORE

        # 1. 回收异步结果
        if session.pending_cmd is not None:
            result = turn.last_cmd_result or ""
            session.cmd_history.append((session.pending_cmd, result))
            self._on_cmd_result(session, session.pending_cmd, result)
            session.pending_cmd = None
        if session.llm_pending:
            # LLM 服务端故障（errorCode=3：503/502/超时）→ **不计入循环/非 JSON 预算**，
            # 直接重试（IKI8HA r29/r30：连续 502 被当成"答非 JSON"提前放弃任务）
            if any(code == 3 for code, _ in turn.errors):
                session.llm_pending = False
                session.stage = ST_LLM
                session.llm_loops = max(0, session.llm_loops - 1)
            else:
                self._on_llm_result(session, turn.llm_resp or "")
                session.llm_pending = False
        if any(code == 2 for code, _ in turn.errors):   # 答案错 → 重新作答
            session.need_refine = True
            session.submitted = False
            session.answer = None
            session.last_error = "; ".join(d for c, d in turn.errors if c == 2)[:200]
            # 关键：提交后 stage=DONE，必须拉回 LLM 重试，否则干等到超时（IKHYQC/IKHYQB 根因）
            session.stage = ST_LLM
            session.llm_loops = max(0, session.llm_loops - 2)  # 给重试留 2 次循环余量

        # 2. 阶段推进
        return self._advance(turn, session, out)

    def _advance(self, turn: Turn, session: TaskSession, out: PlannerOutput) -> PlannerOutput:
        # LLM 给的命令 → 下发沙盒执行（自动锚定工作目录）
        pend = getattr(session, "_pending_llm_cmd", None)
        if pend:
            session._pending_llm_cmd = None  # type: ignore[attr-defined]
            cmd = self._anchor(pend, session)
            out.execute_cmd = cmd
            session.pending_cmd = cmd
            session.cmd_count += 1
            session._pending_kind = "llm"  # type: ignore[attr-defined]
            session.stage = ST_WAIT_CMD
            return out
        # 提交
        if session.stage == ST_SUBMIT:
            if session.answer is None:
                session.stage = ST_LLM
            else:
                out.submit = submit_answer_command(
                    session.answer if isinstance(session.answer, str)
                    else json.dumps(session.answer, ensure_ascii=False)
                )
                session.submitted = True
                session.stage = ST_DONE
                self.completed += 1
                self._maybe_extract_sop(session)
                return out
        if session.stage == ST_DONE:
            return out
        # 等待中
        if session.stage in (ST_WAIT_CMD, ST_WAIT_LLM):
            return out
        # 确定性直答（local_answer，零 LLM）：显式答案字段 / 简单算术
        if session.stage == ST_EXPLORE:
            if not session.local_done:
                session.local_done = True
                ans = self._local_answer(session.task_text)
                if ans is not None:
                    session.answer = ans
                    session.stage = ST_SUBMIT
                    return self._advance(turn, session, out)
            # 健壮探索（单条命令）
            if not session.explore_sent:
                session.explore_sent = True
                cmd = self._explore_cmd(session)
                out.execute_cmd = cmd
                session.pending_cmd = cmd
                session._pending_kind = "explore"  # type: ignore[attr-defined]
                session.stage = ST_WAIT_CMD
                return out
            session.stage = ST_LLM
            return self._advance(turn, session, out)
        # 工程类确定性探测（去 CRLF + ./check）
        if session.stage == ST_PROBE:
            session.probe_sent = True
            cmd = self._probe_cmd(session)
            out.execute_cmd = cmd
            session.pending_cmd = cmd
            session._pending_kind = "probe"  # type: ignore[attr-defined]
            session.stage = ST_WAIT_CMD
            return out
        # 工程类确定性修复（按 check FAIL / spec 生成 mkdir/chmod/sed，零 LLM）
        if session.stage == ST_FIX:
            cmd = session.pending_fix
            session.pending_fix = None
            if cmd is None:
                session.stage = ST_LLM
                return self._advance(turn, session, out)
            session.fix_rounds += 1
            out.execute_cmd = cmd
            session.pending_cmd = cmd
            session._pending_kind = "fix"  # type: ignore[attr-defined]
            session.stage = ST_WAIT_CMD
            return out
        # LLM_LOOP
        if session.stage == ST_LLM:
            # 强制答案触发：循环超限 或 距任务超时 ≤ FORCE_ANSWER_MARGIN 回合
            force = (
                session.force_answer
                or session.llm_loops >= session.max_loops
                or self._at_deadline(turn, session)
            )
            if force:
                # 仅当"已发过强制答案" **且已到截止回合** 才放弃（IKI8HA：服务端 502 后一次空响应
                # 就 ST_DONE → 任务干等到超时）。未到截止则继续给 LLM 机会（force 模式只准给答案）。
                if session.force_sent and self._at_deadline(turn, session):
                    session.stage = ST_DONE
                    return out
                session.force_sent = True
                session.force_answer = True
            out.prompt = self._build_prompt(session)
            session.llm_pending = True
            session.llm_loops += 1
            session.stage = ST_WAIT_LLM
            return out
        return out

    # ---- 命令构造 ----
    def _explore_cmd(self, session: TaskSession) -> str:
        """健壮探索（用户模板）：定位任务文件 + 读同目录文档 + 列目录权限。"""
        name = session.target_name or "task_*.md"
        return (
            f'f=$(find /tmp/selfEvolutionTask -type f -iname "{name}" -print -quit 2>/dev/null); '
            f'[ -n "$f" ] || f=$(find / -maxdepth 10 -type f -iname "{name}" -print -quit 2>/dev/null); '
            'd=$(dirname "$f"); echo "__FILE:$f"; echo "=== TASK ==="; cat "$f" 2>/dev/null; '
            'find "$d" -maxdepth 4 -type f \\( -iname "*.md" -o -iname "*.txt" \\) '
            '! -name "$(basename "$f")" -print 2>/dev/null | while read p; do '
            'echo "=== FILE:$p ==="; cat "$p"; done; '
            'echo "=== LIST ==="; find "$d" -maxdepth 3 -type f -printf \'%p %m\\n\' 2>/dev/null; '
            'echo "__DIR:$d"'
        )

    def _probe_cmd(self, session: TaskSession) -> str:
        """工程类确定性探测：定位 check → 去 CRLF → chmod → ls → ./check（拿 FAIL 清单）。"""
        d = session.task_dir or "."
        return (
            f'c=$(find "{d}" -maxdepth 3 -type f -name check -print -quit 2>/dev/null); '
            'w=$(dirname "$c"); echo "__WS:$w"; cd "$w" && sed -i \'s/\\r$//\' check 2>/dev/null; '
            'chmod +x check 2>/dev/null; ls -la; echo "=== CHECK ==="; ./check 2>&1'
        )

    @staticmethod
    def _anchor(cmd: str, session: TaskSession) -> str:
        """LLM 命令锚定工作目录：未显式 cd 且未用绝对路径时补 `cd "$dir" && `。"""
        c = (cmd or "").strip()
        if not c or not session.task_dir:
            return c
        if re.search(r"(^|&&|;|\|)\s*cd\s", c) or c.startswith("/"):
            return c
        return f'cd "{session.task_dir}" && {c}'

    # ---- 命令结果 ----
    def _on_cmd_result(self, session: TaskSession, cmd: str, result: str) -> None:
        # transcript：LLM 上下文的核心载体（issue IKI8DZ §2）
        session.transcript.append(
            f"COMMAND: {cmd}\nRESULT: {(result or '')[:TRANSCRIPT_RESULT_MAX]}"
        )
        kind = getattr(session, "_pending_kind", None)
        session._pending_kind = None  # type: ignore[attr-defined]
        if kind == "explore":
            self._on_explore(session, result)
        elif kind == "probe":
            self._on_probe(session, result)
        elif kind == "fix":
            self._on_fix(session, result)
        elif config.HARDCODED_ASSIST and (
            self._try_token_submit(session, result)
            or self._try_final_answer_submit(session, result)
            or self._maybe_crlf_fix(session, result)
        ):
            pass
        else:
            session.stage = ST_LLM
        # 家族经验学习放在分类之后（task_type 已确定），按族固化
        self._learn_notes(session, result)

    def _try_final_answer_submit(self, session: TaskSession, result: str) -> bool:
        """命令输出里的 `FINAL_ANSWER {json}` → 直接提交（answer_from_result）。"""
        m = _FINAL_ANSWER.search(result or "")
        if not m:
            return False
        obj = _parse_llm_json(m.group(1))
        if not obj or self._answer_suspect(obj):
            return False
        session.answer = json.dumps(obj, ensure_ascii=False)
        session.stage = ST_SUBMIT
        return True

    def _on_explore(self, session: TaskSession, result: str) -> None:
        session.explore_output = (result or "")[:EXPLORE_LIMIT]
        file_m = _FILE.search(result or "")
        dir_m = _DIR.search(result or "")
        path = (file_m.group(1) if file_m else "").strip()
        if dir_m:
            session.task_dir = dir_m.group(1).strip()
        if path:
            session.files[path.rsplit("/", 1)[-1]] = session.explore_output
        out = session.explore_output
        # 任务类型分类（SOP/经验按 task_type 固化，跨任务复用）
        session.city = self._extract_city(session, out)
        session.task_type = _classify(out or session.task_text)
        session.engineer = session.task_type == "engineering"
        # 硬编码能力（默认开启）：仅当 HARDCODED_ASSIST=True 才走确定性分支
        if config.HARDCODED_ASSIST:
            if self._try_token_submit(session, result):
                return
            if session.engineer and not session.probe_sent:
                session.stage = ST_PROBE
                return
        session.stage = ST_LLM

    # 城市名映射：文件名拼音 + 任务文本中的中文城市（仅作 prompt 线索）
    _CITY_PINYIN = {
        "beijing": "北京", "shanghai": "上海", "guangzhou": "广州", "shenzhen": "深圳",
        "xian": "西安", "nanjing": "南京", "hangzhou": "杭州", "chengdu": "成都",
        "tianjin": "天津", "chongqing": "重庆", "wuhan": "武汉", "suzhou": "苏州",
        "xianyang": "咸阳", "luoyang": "洛阳", "kaifeng": "开封", "datong": "大同",
    }
    _CITY_CN = (
        "北京", "上海", "广州", "深圳", "西安", "南京", "杭州", "成都",
        "天津", "重庆", "武汉", "苏州", "洛阳", "开封", "大同", "沈阳",
    )

    @classmethod
    def _extract_city(cls, session: TaskSession, explore: str) -> str:
        """从任务文件名(拼音)或探索输出(中文)提取 API 任务的城市线索。"""
        for py, cn in cls._CITY_PINYIN.items():
            if py in (session.target_name or "").lower() or py in (session.task_text or "").lower():
                return cn
        for cn in cls._CITY_CN:
            if cn in (explore or "") or cn in (session.task_text or ""):
                return cn
        m = re.search(r"([一-龥]{2,4})市", explore or "")
        return m.group(1) if m else ""

    def _on_probe(self, session: TaskSession, result: str) -> None:
        ws = _WS_DIR.search(result or "")
        if ws and ws.group(1).strip() not in ("", "."):
            session.task_dir = ws.group(1).strip()
        if config.HARDCODED_ASSIST and self._try_token_submit(session, result):
            return
        # 工程类确定性修复（零 LLM）：解析 check FAIL → mkdir/chmod/sed → 再 check
        if session.fix_rounds < 2:
            fix = self._build_fix(session, result)
            if fix is not None:
                session.pending_fix = fix
                session.stage = ST_FIX
                return
        session.stage = ST_LLM

    def _on_fix(self, session: TaskSession, result: str) -> None:
        if config.HARDCODED_ASSIST and self._try_token_submit(session, result):
            return
        if session.fix_rounds < 2:
            fix = self._build_fix(session, result)
            if fix is not None:
                session.pending_fix = fix
                session.stage = ST_FIX
                return
        session.stage = ST_LLM

    def _build_fix(self, session: TaskSession, result: str) -> str | None:
        """解析 check 的 `[FAIL] DIR/LINE` 行（其次 spec）→ 生成修复命令（末尾附 ./check）。"""
        if not config.HARDCODED_ASSIST:
            return None  # 硬编码能力关闭：交给 LLM 修复
        cmds: list[str] = []
        for path, mode in _FAIL_DIR.findall(result or ""):
            cmds.append(f'mkdir -p "{path}" && chmod {mode} "{path}"')
        for file, line, val in _FAIL_LINE.findall(result or ""):
            val = val.strip().strip("`").replace("/", "\\/")
            cmds.append(f"sed -i '{line}s/.*/{val}/' \"{file}\"")
        if not cmds:
            # 回退：从 spec 提取目录权限 + 第 N 行内容
            spec = session.explore_output or ""
            for path, mode in _SPEC_DIR.findall(spec):
                cmds.append(f'mkdir -p "{path}" && chmod {mode} "{path}"')
            conf = self._spec_conf_file(spec)
            for line, val in _SPEC_LINE.findall(spec):
                if conf:
                    val = val.strip().strip("`").replace("/", "\\/")
                    cmds.append(f"sed -i '{line}s/.*/{val}/' \"{conf}\"")
        if not cmds:
            return None
        body = " ; ".join(cmds)
        return f'cd "{session.task_dir or "."}" && {body} ; ./check 2>&1'

    @staticmethod
    def _spec_conf_file(spec: str) -> str | None:
        m = re.search(r"配置文件\s+(\S+\.conf)", spec or "")
        return m.group(1) if m else None

    # ---- 确定性直答（local_answer，零 LLM）----
    def _local_answer(self, text: str) -> str | None:
        """任务文本直接可得的答案：显式答案字段 / 简单算术表达式。"""
        t = text or ""
        m = _EXPLICIT_ANSWER.search(t)
        if m:
            val = m.group(1).strip().strip("\"'`")
            if val and val.lower() not in _ANSWER_PLACEHOLDERS:
                return val
        m = _ARITH.search(t)
        if m:
            val = _safe_eval(m.group(1))
            if val is not None:
                return str(val)
        return None

    def _answer_suspect(self, answer: Any) -> bool:
        """答案安全校验（策略书 §6.4 + plausible_answer）：疑似查询失败/占位符 → 不提交。

        - 空 dict / 空字符串
        - 字符串含 401/403/error/traceback/占位符 xxx
        - 全零 JSON（total_count/types 全空，通常是查询失败被吞成 0）
        """
        if answer in (None, "", {}):
            return True
        text = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        low = text.lower()
        for bad in ("401", "403", "error", "traceback", "unauthorized", "forbidden"):
            if bad in low:
                return True
        if "xxx" in low or "占位" in text:
            return True
        obj = _parse_llm_json(text) if text.strip().startswith("{") else None
        if isinstance(obj, dict) and obj:
            nums = [v for v in obj.values() if isinstance(v, (int, float))]
            lists = [v for v in obj.values() if isinstance(v, list)]
            # 所有数值均为 0 且列表均空 → 视为查询失败
            if nums and all(n == 0 for n in nums) and (not lists or all(len(x) == 0 for x in lists)):
                return True
        return False

    def _try_token_submit(self, session: TaskSession, result: str) -> bool:
        """命令输出里**独立行** `TOKEN: xxx`（真实 check 通过标志）→ 直接提交。

        防误收（issue#26）：任务书示例 `"token": "xxx"` 含占位符，不得当答案。
        """
        m = _TOKEN.search(result or "")
        if not m:
            return False
        value = m.group(1).strip()
        if value.lower() in _TOKEN_PLACEHOLDERS or len(value) < 6:
            return False
        session.answer = json.dumps({"token": value}, ensure_ascii=False)
        session.stage = ST_SUBMIT
        return True

    def _maybe_crlf_fix(self, session: TaskSession, result: str) -> bool:
        """工程类命令因 CRLF/相对路径失败 → 确定性重试（去 CRLF + chmod + ./check）。"""
        if not session.engineer or session.crlf_fixed:
            return False
        low = (result or "").lower()
        if "bad interpreter" not in low and "^m" not in low and "\\r" not in result:
            return False
        session.crlf_fixed = True
        session.stage = ST_PROBE
        return True

    # ---- 家族经验学习（learn_family_notes）----
    def _learn_notes(self, session: TaskSession, result: str) -> None:
        """从命令结果提取可复用事实，按 task_type 跨任务持久化（issue IKI8DZ §5.2）。"""
        text = result or ""
        if not text:
            return
        key = session.task_type or "general"
        notes = self.notes.setdefault(key, [])
        for pat, tpl in _NOTE_PATTERNS:
            m = pat.search(text)
            if not m:
                continue
            msg = tpl.format(m.group(1)) if "{" in tpl else tpl
            if msg not in notes:
                notes.append(msg)

    # ---- LLM 结果 ----
    def _on_llm_result(self, session: TaskSession, text: str) -> None:
        session.last_llm_raw = text or ""
        obj = _parse_llm_json(text)
        if obj is None:                                  # 非 JSON
            session.non_json += 1
            if session.non_json >= MAX_NON_JSON:
                session.stage = ST_DONE                  # 强制结束（不提交）
            else:
                session.stage = ST_LLM
            return
        session.non_json = 0
        cmd = str(obj.get("cmd") or "").strip()
        answer = obj.get("answer")
        finished = bool(obj.get("isFinished"))
        if answer not in (None, "", {}):
            # 答案安全校验（策略书 §6.4）：查询失败/全零/占位符一律不提交，让 LLM 重试
            if self._answer_suspect(answer):
                session.non_json += 1
                session.need_refine = True
                if session.non_json >= MAX_NON_JSON:
                    session.stage = ST_DONE
                else:
                    session.stage = ST_LLM
                return
            session.answer = self._recompute_api_answer(session, answer)
            session.stage = ST_SUBMIT
            return
        if cmd:
            # 强制答案模式 或 命令预算用尽 → 拒绝新命令，只准给答案
            if session.force_answer or session.cmd_count >= session.max_cmds:
                session.force_answer = True
                session.stage = ST_LLM
                return
            session.stage = ST_WAIT_CMD
            session._pending_llm_cmd = cmd  # type: ignore[attr-defined]
            return
        if finished:
            session.stage = ST_DONE
            return
        session.stage = ST_LLM

    # ---- API 记录解析 / 分页完整性 / 提交前重算 ----
    def _transcript_text(self, session: TaskSession) -> str:
        """transcript 注入文本（30k 规则：前 12k + 后 18k）。"""
        body = "\n".join(session.transcript)
        if len(body) <= TRANSCRIPT_MAX:
            return body
        return body[:TRANSCRIPT_HEAD] + TRANSCRIPT_OMIT + body[-TRANSCRIPT_TAIL:]

    @staticmethod
    def _dedup_records(records: list) -> list:
        """记录去重（优先 id，其次 name，最后整体 JSON）。"""
        seen: set = set()
        out: list = []
        for r in records:
            if not isinstance(r, dict):
                continue
            key = r.get("id") or r.get("name") or json.dumps(r, sort_keys=True, ensure_ascii=False)
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
        return out

    def _api_records(self, session: TaskSession) -> tuple[list, int | None]:
        """从 transcript 解析已收割记录与 total_count（供分页提示 / 提交前重算）。"""
        text = self._transcript_text(session)
        records: list = []
        total: int | None = None
        for obj in _iter_json_objects(text):
            if not isinstance(obj, dict):
                continue
            data = obj.get("data")
            recs = pag = None
            if isinstance(data, dict):
                recs = data.get("records")
                pag = data.get("pagination")
            if recs is None:
                recs = obj.get("records")
            if pag is None:
                pag = obj.get("pagination")
            if isinstance(recs, list):
                records.extend(r for r in recs if isinstance(r, dict))
            if isinstance(pag, dict):
                for k in ("total_count", "totalCount", "total", "count"):
                    v = pag.get(k)
                    if isinstance(v, int) and v > 0:
                        total = v
                        break
        return records, total

    def _pagination_note(self, session: TaskSession) -> str:
        """分页不完整 → 反馈给 LLM 继续取（issue IKI8DZ §5.3）。"""
        records, total = self._api_records(session)
        if not records or total is None:
            return ""
        uniq = self._dedup_records(records)
        if len(uniq) >= total:
            return ""
        return (
            f"【分页不完整】transcript 已收集去重记录 {len(uniq)} 条 < total_count {total}；"
            "请继续用更大的 offset/limit 请求剩余记录，取全后再提交答案（不要用不完整页面凑数）。"
        )

    def _recompute_api_answer(self, session: TaskSession, answer: Any) -> Any:
        """提交前用 transcript 已收割记录重算 API 关键字段（仅当记录已取全，且字段已存在）。

        防 LLM 臆造/漏算（issue IKI8DZ §5.4）。仅在 HARDCODED_ASSIST 开启时生效。
        """
        if not config.HARDCODED_ASSIST:
            return answer
        obj = _parse_llm_json(answer) if isinstance(answer, str) else answer
        if not isinstance(obj, dict) or not obj:
            return answer
        records, total = self._api_records(session)
        uniq = self._dedup_records(records)
        if not uniq or (total is not None and len(uniq) < total):
            return answer
        out = dict(obj)
        if "types" in out:
            types = sorted({str(r.get("type")) for r in uniq if r.get("type")})
            if types:
                out["types"] = types
        if "total_count" in out:
            out["total_count"] = len(uniq)
        if "world_heritage_count" in out:
            wh = [r for r in uniq
                  if "世界遗产" in str(r.get("protected_level") or r.get("protection_level") or "")]
            if wh:
                out["world_heritage_count"] = len(wh)
        return json.dumps(out, ensure_ascii=False) if isinstance(answer, str) else out

    # ---- prompt 组装（每次交互都带上下文 + 历史经验）----
    def _build_prompt(self, session: TaskSession) -> str:
        parts = ["=== 任务描述 ===", session.task_text or ""]
        parts.append(f"任务类型(task_type): {session.task_type or 'general'}")
        if session.task_dir:
            parts.append(f"工作目录: {session.task_dir}（命令已自动 cd 到此目录）")
        if session.city:
            parts.append(f"任务线索: 可能涉及城市 `{session.city}`")
        tr = self._transcript_text(session)
        if tr:
            parts += [
                "=== transcript（沙箱命令累积执行记录：含任务文档/API 文档/规范/目录/命令与结果） ===",
                tr,
            ]
        sop = self.sop.get(session.task_type) or self.sop.get(session.task_key)
        if sop:
            parts += [
                "=== 参考 SOP（历史同类型任务沉淀；命令/认证/参数可直接复用，避免重复试错） ===",
                sop[:1500],
            ]
        notes = self.notes.get(session.task_type) or []
        if notes:
            parts += [
                "=== 家族经验笔记（历史任务学到的事实，直接复用，勿再试错） ===",
                "\n".join(notes[-12:]),
            ]
        if session.task_type == "api" and self.api_facts:
            parts.append("=== 跨任务 API 经验（已验证成功，直接复用，勿再横跳） ===")
            parts.append(json.dumps(self.api_facts, ensure_ascii=False))
        if session.task_type == "api":
            note = self._pagination_note(session)
            if note:
                parts.append(note)
        if session.non_json > 0:
            parts.append("你上一次的返回未按要求仅返回JSON，请勿再犯。")
        if session.last_error:
            parts += ["=== 上次提交被判错（必须据此修正答案） ===", session.last_error]
        if session.force_answer:
            parts.append(
                "【强制提交】距任务超时/命令预算已到极限：**必须直接给出 answer，不得再返回 cmd**；"
                "信息不足也要给出当前最佳答案。"
            )
        parts.append(self._contract(session.task_type))
        return "\n".join(parts)

    @staticmethod
    def _contract(task_type: str) -> str:
        base = (
            "请只返回 JSON: {\"cmd\": \"\", \"answer\": \"\", \"isFinished\": true|false}\n"
            "说明：cmd=要执行的 shell 命令（单行；为空则不执行）；"
            "answer=最终答案（JSON 字符串，为空则未完成）；isFinished=任务是否结束。\n"
        )
        if task_type == "api":
            base += (
                "API 类：先读 transcript 里的 API_DOCS / 历史命令与响应，**优先复用已验证成功的认证与参数**；"
                "curl 不通/信息不足时返回一条 curl 命令去取数据（4xx 必须打印 response BODY 以学习真实参数名）；"
                "中文参数用 --data-urlencode 或 urllib.parse.quote 编码（原始中文在 Python URL 中会报 ascii codec 错误）；"
                "**能直接推断出答案时就直接给 answer**，不要再发命令。不要编造数据、不要用不完整页面凑数。\n"
                "已验证的响应格式：{\"code\":200,\"data\":{\"records\":[{...}],"
                "\"pagination\":{\"total_count\":N,\"offset\":0,\"limit\":10}}}\n"
                "  · 记录在 **data.records**（不是 data 本身）；总数在 **data.pagination.total_count**；"
                "`limit` 可能被服务端忽略（每页固定 10 条）→ **必须用 offset=0,10,20... 翻页**，"
                "直到去重记录数 ≥ total_count 再作答（**只看第一页会漏记录 → 统计必错**）。\n"
                "  · **不要写复杂的 python 解析脚本**：直接 `curl -s '<url>'` 打印**完整**原始 JSON，"
                "由你自己阅读 JSON 得出结论；transcript 会自动保留原始输出，**不要用 `head -c` 截断**"
                "（截断会让你看不到后面的记录 → oldest_era 等统计出错）。\n"
                "  · 提交前**重算**（用你已读到的全部记录）：`total_count`=去重记录数；`world_heritage_count`="
                "protected_level 为“世界遗产”的条数；`types`=去重排序；`oldest_era`=**年代最早**的那条记录名，"
                "年代序：旧石器/新石器 < 商周 < 春秋战国 < 秦汉 < 三国 < 南北朝 < 隋唐 < 宋 < 元 < 明清 < 民国 < 现代。"
            )
        elif task_type == "engineering":
            base += (
                "工程修复类：用绝对路径访问 ws_* 工作目录，按 spec.md/check 的 [FAIL] 清单修复"
                "（mkdir -p/chmod/sed 第N行），完成后运行 ./check；"
                "输出含独立行 `TOKEN: xxx` 即代表通过（可直接作为 token 答案提交）。不要重复读取 transcript 里已有的文件。"
            )
        else:
            base += (
                "通用类：复用 transcript 证据，必要时执行命令探测（python/find/grep 等），"
                "完成后输出答案；不要重复读取 transcript 里已有的文件。"
            )
        return base

    # ---- SOP 提取（IKHYTW §12.6：按任务类型固化，跨任务复用）----
    def _maybe_extract_sop(self, session: TaskSession) -> None:
        key = session.task_type or session.task_key or "general"
        cmds = " ; ".join(c for c, _ in session.cmd_history[-6:])
        ans = (session.answer if isinstance(session.answer, str)
               else json.dumps(session.answer, ensure_ascii=False))
        self.sop[key] = (
            f"任务：{session.task_text[:150]}\n命令序列：{cmds[:700]}\n最终答案：{ans[:300]}"
        )
        if key == "api":
            self._learn_api_facts(session)

    def _learn_api_facts(self, session: TaskSession) -> None:
        """从**成功(HTTP 200)**的命令学习认证头/参数名/路径，跨任务复用（避免重复横跳）。"""
        for cmd, result in session.cmd_history:
            if "200" not in (result or "")[:60]:
                continue
            low = cmd.lower()
            if "bearer" in low:
                self.api_facts["auth"] = "Authorization: Bearer <key>"
            elif "x-api-key" in low:
                self.api_facts["auth"] = "X-API-Key: <key>"
            m = re.search(r"[?&](location|city|cityName|name)=", cmd)
            if m:
                self.api_facts["param"] = m.group(1)
            m2 = re.search(r"(/api/[A-Za-z0-9_\-/]+)", cmd)
            if m2:
                self.api_facts["path"] = m2.group(1)

    @staticmethod
    def _task_timeout(turn: Turn) -> int:
        """当前任务点的 timeoutRounds（取距开拓者 ≤1 的那个）。"""
        pioneer = turn.pioneer()
        if pioneer is None:
            return 0
        for t in turn.tasks:
            if t.timeout_rounds > 0 and distance(pioneer.pos, t.pos) <= 1:
                return t.timeout_rounds
        return 0

    @staticmethod
    def _at_deadline(turn: Turn, session: TaskSession) -> bool:
        """距任务超时 ≤ FORCE_ANSWER_MARGIN 回合 → 强制只给答案。"""
        if session.timeout_rounds <= 0:
            return False
        remaining = session.timeout_rounds - (turn.round_no - session.accept_round)
        return remaining <= FORCE_ANSWER_MARGIN

    @staticmethod
    def _task_key(task_text: str) -> str:
        """任务指纹（用于 SOP 兜底匹配）：取任务文本里的关键词/文件名。"""
        names = _extract_file_names(task_text)
        if names:
            return names[0].rsplit("_", 1)[-1].rsplit(".", 1)[0]  # task_1_alpha.md → alpha
        return (task_text or "")[:20]


def _iter_json_objects(text: str):
    """迭代文本里所有平衡的 {...} 子串并尝试解析。"""
    depth, start = 0, -1
    for i, ch in enumerate(text or ""):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        yield json.loads(text[start:i + 1])
                    except (json.JSONDecodeError, ValueError):
                        pass


_ANSWER_KEYS = {
    "token", "total_count", "totalcount", "world_heritage_count", "types",
    "oldest_era", "city", "answer", "result", "value", "count", "total",
}
_ERROR_KEYS = {"status", "error", "message", "code"}


def _looks_like_answer(obj, task_text: str) -> bool:
    """命令输出里的 JSON 是否"像答案"（防误收 API 原始记录/错误报文）。"""
    if not isinstance(obj, dict) or not obj:
        return False
    keys = {str(k).lower() for k in obj}
    if "status" in keys and str(obj.get("status", "")).lower() in ("error", "fail", "failed"):
        return False
    if keys & _ERROR_KEYS and "message" in keys and not (keys & _ANSWER_KEYS):
        return False
    if keys & _ANSWER_KEYS:
        return True
    low = (task_text or "").lower()
    return any(len(k) >= 2 and k in low for k in keys)
