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
STATE_TASK_WAIT_ACCEPT = "TASK_WAIT_ACCEPT"
STATE_TASK_WORK = "TASK_WORK"
STATE_RETURN_HOME = "RETURN_HOME"

DUSK_MARGIN = 3  # 黄昏返程安全余量（回合）


class PioneerFSM:
    def __init__(self) -> None:
        self.state = STATE_GUARD
        self.task_point: Pos | None = None
        self.accept_retries = 0
        self.failed_task_points: set = set()  # accept FAIL 的任务点终身回避（防封号循环）

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
            # 移动中每回合校验任务点有效性（《自进化策略》MOVING）
            task = next((t for t in turn.tasks if t.pos == self.task_point), None)
            if task is None or not task.is_valid or task.cooldown_rounds > 0:
                ctx.note_pioneer("task_point_invalid_abort")
                self.state = STATE_GUARD
                self.task_point = None
                return None
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
            self.state = STATE_TASK_WAIT_ACCEPT
            self.accept_retries = 0
            return accept_task_command()
        if self.state == STATE_TASK_WAIT_ACCEPT:
            if turn.phase_task:
                self.state = STATE_TASK_WORK
                return None
            # acceptTask FAIL 计 errorCode 4（指令错误），5 次封号 → 立即放弃并终身回避该点
            if turn.last_action_results.get(pioneer.unit_id, True) is False:
                ctx.note_pioneer("accept_fail_no_retry")
                self.failed_task_points.add(self.task_point)
                self.state = STATE_GUARD
                self.task_point = None
                return None
            self.accept_retries += 1
            if self.accept_retries > 1:
                ctx.note_pioneer("accept_no_confirm_abort")
                self.failed_task_points.add(self.task_point)
                self.state = STATE_GUARD
                self.task_point = None
                return None
            return accept_task_command()  # 无 FAIL 但未确认 → 最多重试 1 次
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
        cands = [
            t for t in turn.tasks
            if t.is_valid and t.cooldown_rounds == 0 and t.pos not in self.failed_task_points
        ]
        if not cands:
            return None
        cands.sort(key=lambda t: (distance(pioneer.pos, t.pos), -t.score_reward))
        return cands[0].pos
