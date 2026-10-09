"""IKKJ2E/IKKJ2J/IKKJ2K 复盘实施回归（2026-10-09 深夜用户裁决）。

覆盖：
- R1（Q5 回退）：布局恢复 14 面全封闭主环（无背墙/门）——三场 day1 夜基地被
  推平的直接原因就是背墙+门 day1 建不完、开口列整夜敞开。
- R2（Q7 回退）：三门火箭 + pioneer 单操控（电磁炮+挖矿工双操控 errorCode=4）。
- R3（P0-B）：任务绝对优先——有任务可接时 pioneer 不进买券/升级分支。
- R4（P0-B）：时间预算不含回家路程——离家远但来得及接+做的任务照接
  （IKKJ2E r51 站在任务点旁却提前回家，每天 240-480 分损失主因）。
- R5（P1-A）：TASK_WORK 无 LLM 推进超 12 轮 → 放弃任务拉黑任务点
  （IKKJ2K 任务1 卡 12+ 轮被平台超时终止，20 轮全废）。
"""
import unittest

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.protocol import Pos, Turn
from agent.brain import Brain
from agent.fsm_pioneer import PioneerFSM, STATE_GUARD
from agent.planners.layout import compute_layout


def _turn(sim: SimWorld, round_no: int | None = None) -> Turn:
    payload = sim.payload()
    if round_no is not None:
        payload["roundNo"] = round_no
    return Turn.load(payload)


class TestQ5Rollback(unittest.TestCase):
    """R1/R2：Q5 背墙回退 + Q7 三门火箭回退。"""

    def test_layout_pure_14_ring(self):
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone"})
        turn = _turn(sim, 1)
        layout = compute_layout(turn, "W", None)
        self.assertEqual(len(layout.wall_cells), 14, "应恢复 14 面全封闭主环")

    def test_day1_night_walls_closed(self):
        """day1 全天跑完（含入夜）后主环应闭合——推平事故的端到端防线。"""
        sim = SimWorld(station_pos=(10, 24),
                       mines={(6, 22): "stone", (7, 26): "stone"})
        brain = Brain()
        for _ in range(200):
            r, _ = brain.decide(sim.payload())
            sim.apply(r)
            sim.advance()
        built = {(w["pos"]["x"], w["pos"]["y"]) for w in sim.walls()}
        expected = {(c.x, c.y) for c in brain.layout.wall_cells}
        self.assertTrue(expected <= built,
                        f"主环 14 面应建满, 缺: {expected - built}")

    def test_no_railgun_built(self):
        sim = SimWorld(station_pos=(10, 24),
                       mines={(6, 22): "stone", (7, 26): "stone"})
        brain = Brain()
        for _ in range(70):
            r, _ = brain.decide(sim.payload())
            sim.apply(r)
            sim.advance()
        kinds = [w["roleType"] for w in sim.weapons()]
        self.assertTrue(kinds, "应有武器建成")
        self.assertNotIn("railgun", kinds, "Q7 回退：不再建电磁炮")


class TestTaskPriority(unittest.TestCase):
    """R3/R4：任务绝对优先 + 时间预算不含回家。"""

    def test_task_beats_upgrade_when_far_shop(self):
        """有任务可接 + 商店很远 → 仍走任务（不跑远路买券）。"""
        sim = SimWorld(
            station_pos=(10, 24),
            mines={(6, 22): "stone", (8, 20): "copper"},
            shop=(38, 2),   # 极远
            tasks=[{"pos": (14, 14), "text": "T", "scoreReward": 120,
                    "goldReward": 120, "timeoutRounds": 60}],
        )
        brain = Brain()
        r, trace = brain.decide(sim.payload())
        p = trace.get("pioneer") or {}
        self.assertIn(p.get("state"), ("TASK_TRAVEL", "TASK_ACCEPT", "TASK_WAIT_ACCEPT"),
                      f"任务可接时应直接进入任务态: {p}")

    def test_far_from_home_task_still_taken(self):
        """R4：pioneer 离家远、但任务就在身边且天黑还早 → 必须接（预算不含 home）。"""
        sim = SimWorld(
            station_pos=(10, 24),
            mines={(6, 22): "stone", (8, 20): "copper"},
            tasks=[{"pos": (14, 14), "text": "T", "scoreReward": 120,
                    "goldReward": 120, "timeoutRounds": 60}],
        )
        brain = Brain()
        r, _ = brain.decide(sim.payload())
        sim.apply(r)
        sim.advance()
        # 把 pioneer 丢到远处（IKKJ2E r51 场景：离家 ~14 步，任务点就在旁边）
        sim.role(10011)["pos"] = {"x": 17, "y": 12}
        sim.round_no = 51   # 距天黑 20 轮
        r, trace = brain.decide(sim.payload())
        p = trace.get("pioneer") or {}
        self.assertIn(p.get("state"), ("TASK_TRAVEL", "TASK_ACCEPT", "TASK_WAIT_ACCEPT"),
                      f"预算不含 home 后应接下身边任务: {p}")

    def test_no_task_allows_nearby_upgrade(self):
        """无任务可接 + 商店在预算内 → 允许买券升级（升级链不死）。"""
        sim = SimWorld(
            station_pos=(10, 24),
            mines={(6, 22): "stone", (8, 20): "copper"},
            shop=(12, 12),   # 近店
            tasks=[],        # 无任务
        )
        brain = Brain()
        for _ in range(70):
            r, _ = brain.decide(sim.payload())
            sim.apply(r)
            sim.advance()
        sim.weapons()[0]["health"] = 500
        sim.gold = 200
        for _ in range(100):
            r, _ = brain.decide(sim.payload())
            sim.apply(r)
            sim.advance()
        levels = [w["level"] for w in sim.weapons()]
        self.assertIn(2, levels, f"无任务窗口内应完成近店升级: {levels}")


class TestTaskStallAbort(unittest.TestCase):
    """R5：TASK_WORK 无 LLM 推进超限 → 放弃任务。"""

    def test_stalled_task_abandoned_after_limit(self):
        # monkeypatch：planner 永不产出 submit（模拟 LLM 响应丢失/求解器卡死）
        from agent.planners.task import PlannerOutput
        sim = SimWorld(
            station_pos=(10, 24),
            mines={(6, 22): "stone", (8, 20): "copper"},
            tasks=[{"pos": (14, 14), "text": "T", "scoreReward": 120,
                    "goldReward": 120, "timeoutRounds": 60}],
        )
        brain = Brain()
        # 等任务接上（TASK_WORK + phase_task）
        entered = False
        for _ in range(30):
            r, trace = brain.decide(sim.payload())
            sim.apply(r)
            sim.advance()
            if brain.pioneer_fsm.state == "TASK_WORK" and trace.get("pioneer"):
                entered = True
                break
        self.assertTrue(entered, "测试前提：任务应已接取进入 TASK_WORK")
        brain.task_planner.work = lambda turn, session: PlannerOutput()
        brain._task_work_since = sim.round_no - 13   # 已卡 13 轮
        stall_seen = False
        for _ in range(3):
            r, trace = brain.decide(sim.payload())
            if trace.get("task_stall_abort") is not None:
                stall_seen = True
                break
            sim.apply(r)
            sim.advance()
        self.assertTrue(stall_seen, "任务卡死应触发 stall 放弃")
        self.assertEqual(brain.pioneer_fsm.state, STATE_GUARD)
        # 被拉黑的任务点不再被选中
        self.assertIn(Pos(14, 14), brain.pioneer_fsm.failed_task_points)


if __name__ == "__main__":
    unittest.main()
