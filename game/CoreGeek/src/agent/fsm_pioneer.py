"""L4 PioneerFSM：任务流（P5）+ GUARD 夜防。

白天状态机：
  GUARD →（有可用任务点）→ TASK_TRAVEL → TASK_ACCEPT → TASK_WORK（驻留任务点）
  黄昏返程预算耗尽 → RETURN_HOME → GUARD
夜间由 brain 调用 move_to_guard + 控炮。
任务求解（LLM/沙盒/提交）由 planners/task.py 完成，FSM 只管走位与生命周期。
"""
from __future__ import annotations

from typing import Any

from .path import find_path, next_step, step_toward
from .protocol import Pos, Turn, Unit, accept_task_command, distance, move_command

STATE_GUARD = "GUARD"
STATE_TASK_TRAVEL = "TASK_TRAVEL"
STATE_TASK_ACCEPT = "TASK_ACCEPT"
STATE_TASK_WORK = "TASK_WORK"
STATE_RETURN_HOME = "RETURN_HOME"

DUSK_MARGIN = 3  # 黄昏返程安全余量（回合）


class PioneerFSM:
    def __init__(self) -> None:
        self.state = STATE_GUARD
        self.task_point: Pos | None = None

    # 夜间走位（P1 起沿用）
    def move_to_guard(self, turn: Turn, pioneer: Unit, cp: Pos, ctx) -> dict[str, Any] | None:
        if pioneer.pos == cp:
            return None
        step = next_step(turn, pioneer, cp, ctx.reserved)
        return move_command(step) if step is not None else None

    # 白天状态机
    def day_cmd(self, turn: Turn, pioneer: Unit, cp: Pos, ctx) -> dict[str, Any] | None:
        cmd = self._day_cmd(turn, pioneer, cp, ctx)
        ctx.trace["pioneer"] = {
            "state": self.state,
            "task_point": self.task_point.dump() if self.task_point else None,
            "cmd": cmd.get("action") if cmd else None,
        }
        return cmd

    def _day_cmd(self, turn: Turn, pioneer: Unit, cp: Pos, ctx) -> dict[str, Any] | None:
        # 1. 黄昏返程预算：路程+余量 ≥ 剩余白天 → 立即回家（离开任务点=任务结束）
        if self.state != STATE_GUARD:
            travel = self._travel_rounds(turn, pioneer, cp, ctx)
            if turn.rounds_until_night <= travel + DUSK_MARGIN:
                if self.state != STATE_RETURN_HOME:
                    ctx.note_pioneer("dusk_return_abort_task")
                self.state = STATE_RETURN_HOME
                self.task_point = None
        # 2. 状态机
        if self.state == STATE_GUARD:
            target = self._choose_task_point(turn, pioneer)
            if target is None:
                return self.move_to_guard(turn, pioneer, cp, ctx)
            self.task_point = target
            self.state = STATE_TASK_TRAVEL
        if self.state == STATE_TASK_TRAVEL:
            if distance(pioneer.pos, self.task_point) <= 1:
                self.state = STATE_TASK_ACCEPT
            else:
                step = step_toward(turn, pioneer, self.task_point, ctx.reserved)
                if step is None:
                    ctx.note_pioneer("task_point_unreachable")
                    self.state = STATE_GUARD
                    self.task_point = None
                    return None
                return move_command(step)
        if self.state == STATE_TASK_ACCEPT:
            self.state = STATE_TASK_WORK
            return accept_task_command()
        if self.state == STATE_TASK_WORK:
            if not turn.phase_task:
                ctx.note_pioneer("task_end")
                self.state = STATE_GUARD
                self.task_point = None
                return None
            return None  # 驻留任务点；求解由 TaskPlanner 异步推进
        if self.state == STATE_RETURN_HOME:
            if pioneer.pos == cp:
                self.state = STATE_GUARD
                return None
            step = next_step(turn, pioneer, cp, ctx.reserved)
            if step is None:
                step = next_step(turn, pioneer, cp)  # 返程优先，忽略预留
            return move_command(step) if step is not None else None
        return None

    def _travel_rounds(self, turn: Turn, pioneer: Unit, cp: Pos, ctx) -> int:
        path = find_path(turn, pioneer, cp, ctx.reserved)
        if path:
            return len(path) - 1
        return distance(pioneer.pos, cp)

    def _choose_task_point(self, turn: Turn, pioneer: Unit) -> Pos | None:
        cands = [t for t in turn.tasks if t.is_valid and t.cooldown_rounds == 0]
        if not cands:
            return None
        cands.sort(key=lambda t: (distance(pioneer.pos, t.pos), -t.score_reward))
        return cands[0].pos
