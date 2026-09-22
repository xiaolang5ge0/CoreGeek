"""L3 TaskPlanner：自进化任务（严格按 issue#21《自进化任务策略》重构）。

7 阶段 FSM：
  FIND_FILES → READ_FILES → LLM_LOOP → WAIT_CMD_RESULT / WAIT_LLM → SUBMIT_ANSWER → COMPLETED

要点：
- 文件递归读取：从 phase_task 提取 .md 文件名 → find 定位 → cat 读取 → 若内容引用其他 .md → 继续查找
- LLM 交互：每回合把 任务描述+文件内容+命令历史+SOP 组装成 prompt，要求 LLM 只返回 JSON
      {"cmd": "", "answer": "", "isFinished": true|false}
  循环：有 cmd → 沙盒执行 → 带结果回到 LLM；有 answer/isFinished → 提交；空 JSON → 重试（超 3 次强制结束）
- 容错：连续 3 次非 JSON → 强制结束（不提交）；错误回复回传下次 prompt；JSON 解析容忍（先 loads 再正则提 {...}）
- SOP 自进化：第一天完成前 2 个任务后提取 SOP；第二天起 prompt 附带匹配 SOP
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..protocol import Turn, submit_answer_command

# ---- 阶段 ----
ST_FIND = "FIND_FILES"
ST_READ = "READ_FILES"
ST_LLM = "LLM_LOOP"
ST_WAIT_CMD = "WAIT_CMD_RESULT"
ST_WAIT_LLM = "WAIT_LLM"
ST_SUBMIT = "SUBMIT_ANSWER"
ST_DONE = "COMPLETED"

MAX_NON_JSON = 3       # 连续非 JSON 上限 → 强制结束
MAX_LLM_LOOPS = 14     # LLM 循环上限（防死循环）

_MD_NAME = re.compile(r"[A-Za-z0-9_\-/]+\.md")
_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def _extract_md_names(text: str) -> list[str]:
    out = []
    for m in _MD_NAME.findall(text or ""):
        name = m.rsplit("/", 1)[-1]      # 只要文件名
        if name not in out:
            out.append(name)
    return out


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


@dataclass
class TaskSession:
    stage: str = ST_FIND
    task_text: str = ""
    task_key: str = ""                                   # SOP 匹配键
    find_queue: list = field(default_factory=list)       # 待 find 的 .md 文件名
    read_queue: list = field(default_factory=list)       # 待 cat 的 .md 文件名
    files: dict = field(default_factory=dict)            # 文件名 → 内容
    pending_find: str | None = None
    pending_read: str | None = None
    pending_cmd: str | None = None
    cmd_history: list = field(default_factory=list)      # [(cmd, result)]
    llm_pending: bool = False
    last_llm_raw: str = ""
    non_json: int = 0
    llm_loops: int = 0
    answer: Any = None
    submitted: bool = False
    need_refine: bool = False

    def reset(self) -> None:
        self.__init__()


@dataclass
class PlannerOutput:
    prompt: str = ""
    execute_cmd: str = ""
    submit: dict | None = None


class TaskPlanner:
    def __init__(self) -> None:
        self.sop: dict[str, str] = {}       # task_key → SOP 文本（跨任务复用）
        self.completed: int = 0             # 已完成任务数（用于第一天提取 SOP）

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
            session.find_queue = _extract_md_names(turn.phase_task)
            session.stage = ST_FIND

        # 1. 回收异步结果
        if session.pending_cmd is not None:
            result = turn.last_cmd_result or ""
            session.cmd_history.append((session.pending_cmd, result))
            self._on_cmd_result(session, session.pending_cmd, result)
            session.pending_cmd = None
        if session.llm_pending:
            self._on_llm_result(session, turn.llm_resp or "")
            session.llm_pending = False
        if any(code == 2 for code, _ in turn.errors):   # 答案错 → 重新作答
            session.need_refine = True
            session.submitted = False
            session.answer = None

        # 2. 阶段推进
        return self._advance(turn, session, out)

    def _advance(self, turn: Turn, session: TaskSession, out: PlannerOutput) -> PlannerOutput:
        # LLM 给的命令 → 下发沙盒执行
        pend = getattr(session, "_pending_llm_cmd", None)
        if pend:
            session._pending_llm_cmd = None  # type: ignore[attr-defined]
            out.execute_cmd = pend
            session.pending_cmd = pend
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
        # FIND_FILES
        if session.stage == ST_FIND:
            if session.pending_find is None:
                if session.find_queue:
                    session.pending_find = session.find_queue.pop(0)
                else:
                    session.stage = ST_READ
                    return self._advance(turn, session, out)
            name = session.pending_find
            session.stage = ST_WAIT_CMD
            out.execute_cmd = f'find / -maxdepth 10 -name "{name}" 2>/dev/null | head -5'
            session.pending_cmd = out.execute_cmd
            session._pending_kind = "find"  # type: ignore[attr-defined]
            return out
        # READ_FILES
        if session.stage == ST_READ:
            if session.pending_read is None:
                if session.read_queue:
                    session.pending_read = session.read_queue.pop(0)
                else:
                    session.stage = ST_LLM
                    return self._advance(turn, session, out)
            name = session.pending_read
            session.stage = ST_WAIT_CMD
            out.execute_cmd = f'cat "$(find / -maxdepth 10 -name "{name}" 2>/dev/null | head -1)"'
            session.pending_cmd = out.execute_cmd
            session._pending_kind = "read"  # type: ignore[attr-defined]
            return out
        # LLM_LOOP
        if session.stage == ST_LLM:
            if session.llm_loops >= MAX_LLM_LOOPS:
                session.stage = ST_DONE
                return out
            out.prompt = self._build_prompt(session)
            session.llm_pending = True
            session.llm_loops += 1
            session.stage = ST_WAIT_LLM
            return out
        return out

    # ---- 命令结果 ----
    def _on_cmd_result(self, session: TaskSession, cmd: str, result: str) -> None:
        kind = getattr(session, "_pending_kind", None)
        session._pending_kind = None  # type: ignore[attr-defined]
        if kind == "find":
            paths = [p for p in (result or "").splitlines() if p.strip().endswith(".md")]
            name = session.pending_find
            session.pending_find = None
            if name and name not in session.read_queue and name not in session.files:
                session.read_queue.append(name)
            session.stage = ST_FIND  # 继续找下一个
            return
        if kind == "read":
            name = session.pending_read
            session.pending_read = None
            if name:
                session.files[name] = (result or "")[:4000]
                # 递归：内容引用其他 .md → 加入查找队列
                for ref in _extract_md_names(result):
                    if ref != name and ref not in session.files and ref not in session.find_queue:
                        session.find_queue.append(ref)
            session.stage = ST_READ if not session.find_queue else ST_FIND
            return
        # LLM 命令结果 → 回 LLM 循环
        session.stage = ST_LLM

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
            session.answer = answer
            session.stage = ST_SUBMIT
            return
        if cmd and not cmd.startswith("cat "):
            session.stage = ST_WAIT_CMD
            session._pending_llm_cmd = cmd  # type: ignore[attr-defined]
            return
        if finished:
            session.stage = ST_DONE
            return
        session.stage = ST_LLM

    # ---- prompt 组装（严格按 MD 格式）----
    def _build_prompt(self, session: TaskSession) -> str:
        parts = ["=== 任务描述 ===", session.task_text or ""]
        for name, content in list(session.files.items())[-6:]:
            parts += [f"=== 文件: {name} ===", content[:2000]]
        if session.cmd_history:
            cmd, result = session.cmd_history[-1]
            parts += ["=== 命令执行结果 ===", f"命令: {cmd}", f"结果: {(result or '')[:2000]}"]
        sop = self.sop.get(session.task_key)
        if sop:
            parts += ["=== 参考 SOP（历史任务沉淀） ===", sop[:1500]]
        if session.non_json > 0:
            parts.append("你上一次的返回未按要求仅返回JSON，请勿再犯。")
        parts.append(
            "请只返回 JSON: {\"cmd\": \"\", \"answer\": \"\", \"isFinished\": true|false}\n"
            "说明：cmd=要执行的 shell 命令（如 curl API 调用，为空则不执行）；"
            "answer=最终答案(JSON字符串，为空则未完成)；isFinished=任务是否结束。\n"
            "分页提示：关注分页参数有效性，优先用 limit/offset 或对齐响应字段。"
        )
        return "\n".join(parts)

    # ---- SOP 提取（第一天完成前 2 个任务后）----
    def _maybe_extract_sop(self, session: TaskSession) -> None:
        if self.completed > 2:
            return
        cmds = " ; ".join(c for c, _ in session.cmd_history[-5:])
        ans = (session.answer if isinstance(session.answer, str)
               else json.dumps(session.answer, ensure_ascii=False))
        self.sop[session.task_key] = (
            f"任务：{session.task_text[:200]}\n命令序列：{cmds[:600]}\n最终答案：{ans[:400]}"
        )

    @staticmethod
    def _task_key(task_text: str) -> str:
        """任务类型指纹（用于 SOP 匹配）：取任务文本里的关键词/文件名。"""
        names = _extract_md_names(task_text)
        if names:
            return names[0].rsplit("_", 1)[-1].replace(".md", "")  # task_1_alpha.md → alpha
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
