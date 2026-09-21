"""L3 TaskPlanner：自进化任务求解器（LLM/沙盒异步 + Multi Submit + Task Memory）。

机制依据（接口文档/任务书）：
- prompt → 下回合 llmResp；executeCmd → 下回合 lastCmdResult（仅任务期间可用）
- 任务期间 LLM 调用不计入每日 3 次限制
- 超时按历史最高通过率结算 → 边做边交（Multi Submit），出错再修正
求解循环：ANALYZE(LLM出计划) → RUN_COMMANDS(沙盒探索) → SUBMIT → 错误则 REFINE 再交
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..protocol import Turn, submit_answer_command

ANALYZE_PROMPT = (
    "你在生存塔防比赛中完成探索任务。只输出 JSON，不要输出其他内容。\n"
    "任务原文：\n{task}\n"
    "输出格式：{\"analysis\":\"...\",\"commands\":[\"要执行的shell命令\"],"
    "\"answer\":{\"字段名\":\"值\"},\"done\":true或false}\n"
    "信息不足时 commands 给探索命令、done=false；能作答时 done=true 且 answer 完整。"
)

COMPILE_PROMPT = (
    "你在生存塔防比赛中完成探索任务。只输出 JSON，不要输出其他内容。\n"
    "任务原文：\n{task}\n"
    "已执行命令与输出：\n{history}\n"
    "请据输出组装最终答案：{\"analysis\":\"...\",\"commands\":[],"
    "\"answer\":{...},\"done\":true}"
)

REFINE_PROMPT = (
    "你在生存塔防比赛中完成探索任务。只输出 JSON，不要输出其他内容。\n"
    "任务原文：\n{task}\n"
    "已提交的答案被判错或不完全正确。历史命令与输出：\n{history}\n"
    "上次答案：{last_answer}\n错误反馈：{error}\n"
    "请修正并输出完整 JSON：{\"analysis\":\"...\",\"commands\":[],"
    "\"answer\":{...},\"done\":true或false}"
)


@dataclass
class TaskSession:
    """Task Memory：单任务会话的全部上下文。"""

    task_text: str = ""
    started_round: int = 0
    plan: dict | None = None
    commands_queue: list = field(default_factory=list)
    commands_run: list = field(default_factory=list)  # [(cmd, result)]
    answer: dict | None = None
    llm_pending: bool = False
    pending_cmd: str | None = None
    submitted: bool = False
    need_refine: bool = False
    idle_rounds: int = 0

    def reset(self) -> None:
        self.__init__()


@dataclass
class PlannerOutput:
    prompt: str = ""
    execute_cmd: str = ""
    submit: dict | None = None


def parse_plan(text: str) -> dict | None:
    """从 LLM 响应中防御性提取第一个 JSON 对象。"""
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        plan = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return plan if isinstance(plan, dict) else None


def _history(session: TaskSession, limit: int = 6, chars: int = 2000) -> str:
    items = session.commands_run[-limit:]
    text = "\n".join(f"$ {cmd}\n{(result or '')[:300]}" for cmd, result in items)
    return text[:chars]


class TaskPlanner:
    def work(self, turn: Turn, session: TaskSession) -> PlannerOutput:
        out = PlannerOutput()
        if not turn.phase_task:
            session.reset()
            return out
        if turn.phase_task != session.task_text:  # 新任务 → 重置会话
            session.reset()
            session.task_text = turn.phase_task
            session.started_round = turn.round_no

        # 1. 回收异步结果
        if session.pending_cmd is not None:
            session.commands_run.append((session.pending_cmd, turn.last_cmd_result))
            session.pending_cmd = None
        if session.llm_pending:
            plan = parse_plan(turn.llm_resp)
            if plan is not None:
                session.plan = plan
                session.commands_queue = [str(c) for c in (plan.get("commands") or [])]
                if isinstance(plan.get("answer"), dict) and plan["answer"]:
                    session.answer = plan["answer"]
            session.llm_pending = False
        if any(code == 2 for code, _ in turn.errors):
            session.need_refine = True
            session.submitted = False

        # 2. 推进求解循环
        if session.plan is None:
            out.prompt = ANALYZE_PROMPT.replace("{task}", session.task_text)
            session.llm_pending = True
        elif session.commands_queue:
            cmd = session.commands_queue.pop(0)
            out.execute_cmd = cmd
            session.pending_cmd = cmd
        elif session.need_refine:
            out.prompt = (
                REFINE_PROMPT.replace("{task}", session.task_text)
                .replace("{history}", _history(session))
                .replace(
                    "{last_answer}",
                    json.dumps(session.answer or {}, ensure_ascii=False),
                )
                .replace("{error}", ";".join(d for c, d in turn.errors if c == 2))
            )
            session.llm_pending = True
            session.need_refine = False
        elif session.answer and not session.submitted:
            out.submit = submit_answer_command(
                json.dumps(session.answer, ensure_ascii=False)
            )
            session.submitted = True
        elif session.answer is None:
            out.prompt = COMPILE_PROMPT.replace("{task}", session.task_text).replace(
                "{history}", _history(session)
            )
            session.llm_pending = True
        else:
            session.idle_rounds += 1
            if session.idle_rounds >= 4:  # 静默多轮后让 LLM 复查是否可提升通过率
                out.prompt = COMPILE_PROMPT.replace("{task}", session.task_text).replace(
                    "{history}", _history(session)
                )
                session.llm_pending = True
                session.idle_rounds = 0
        return out
