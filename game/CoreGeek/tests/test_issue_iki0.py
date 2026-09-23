"""IKI0RT / IKI0Q8 回归：
- 开拓者卡在 RETURN_HOME → 恢复行动（IKI0Q8 根因）
- WallFixer 备货优先级高于围墙升级任务（IKI0RT）
- 建完墙环后修理工常备 ≥5 石头（IKI0RT）
- 任务命令锚定 `_anchor` 正则生效（命令前 cd 检测）
"""
import unittest

import _bootstrap  # noqa: F401

from agent import config
config.HARDCODED_ASSIST = False

from harness import SimWorld
from agent.brain import Brain, _Ctx
from agent.fsm_pioneer import PioneerFSM, STATE_RETURN_HOME
from agent.fsm_worker import WorkerFSM, ROLE_REPAIRER
from agent.planners.task import TaskPlanner, TaskSession
from agent.planners.upgrade import UpgradePlanner
from agent.protocol import Pos, Turn

DAY1 = 70


def run_rounds(brain, sim, n):
    for _ in range(n):
        r, _ = brain.decide(sim.payload())
        sim.apply(r)
        sim.advance()


class TestPioneerRecover(unittest.TestCase):
    def test_recovers_from_stuck_return_home(self):
        """IKI0Q8：开拓者归位受阻卡在 RETURN_HOME → 次日应恢复行动（买券/做任务）。"""
        sim = SimWorld(station_pos=(30, 10), mines={})
        sim.round_no = 131          # Day2 首回合（round_in_day==0 → 重置归位粘性）
        sim.roles.append(sim._role(88888, 32, 6, "rocket", 1000, level=1))
        turn = Turn.load(sim.payload())
        cp = Pos(12, 23)            # 故意给一个到不了的 CP（模拟归位受阻）
        fsm = PioneerFSM()
        fsm.state = STATE_RETURN_HOME
        fsm.returning = True
        fsm.return_since = 100      # 已经"归位"很久
        ctx = _Ctx({})
        ctx.reserved = set()
        ctx.gunner_upgrade = (Pos(32, 6), "weapon")
        fsm._day_cmd(turn, turn.pioneer(), cp, ctx)
        self.assertNotEqual(fsm.state, STATE_RETURN_HOME, "不应再卡在 RETURN_HOME")


class TestWallFixerAboveUpgrade(unittest.TestCase):
    def test_wallfixer_priority_above_wall_upgrade(self):
        """IKI0RT：D4 修理工必须先备围墙修复包，再买升级券/做升级任务。"""
        sim = SimWorld(station_pos=(10, 24), mines={})
        sim.round_no = 391          # Day4
        for i, pos in enumerate([(9, 20), (10, 20), (9, 21)]):
            sim.roles.append(sim._role(90000 + i, pos[0], pos[1], "rocket", 1500, level=2))
        # 目标 L2 的墙（front='W' → dx=2 中间）保持 L1 → 产生 L2 围墙任务(15)
        for i, pos in enumerate([(12, 22), (12, 23), (12, 24), (12, 25)]):
            sim.roles.append(sim._role(91000 + i, pos[0], pos[1], "wall", 1000, level=1))
        sim.gold = 700
        turn = Turn.load(sim.payload())
        ms = UpgradePlanner().plan(turn, cp=Pos(9, 23), front="W")
        fixer = [m for m in ms if m.voucher == "WallFixer"]
        wall = [m for m in ms if m.kind == "wall"]
        self.assertTrue(fixer)
        self.assertTrue(wall)
        self.assertLess(
            fixer[0].priority, min(m.priority for m in wall),
            "修复包优先级必须高于围墙升级任务",
        )


class TestRepairerStoneBackup(unittest.TestCase):
    def test_repairer_mines_stone_after_ring(self):
        """IKI0RT：墙环建完（walls_left=0）后，修理工应常备 ≥5 石头。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (8, 20): "copper"})
        sim.add_mine((6, 22), "stone", remaining=100)
        sim.add_mine((8, 20), "copper", remaining=100)
        sim.round_no = 261          # Day3
        turn = Turn.load(sim.payload())
        worker = next(u for u in turn.ours if u.kind == "worker")
        fsm = WorkerFSM(worker.unit_id)
        fsm.role = ROLE_REPAIRER
        ctx = _Ctx({})
        ctx.reserved = set()
        ctx.dusk_avoid = False
        ctx.walls_left = 0          # 墙环已建完
        ctx.share_mines = False
        fsm._build_mine(turn, worker, ctx, prefer="money")
        self.assertEqual(
            turn.zones.get(fsm.mine), "stone",
            "墙环建完后修理工应采石常备（而非只采钱矿）",
        )


class TestAnchorRegex(unittest.TestCase):
    def test_anchor_adds_cd_when_missing(self):
        s = TaskSession(task_dir="/tmp/ws_1")
        self.assertEqual(
            TaskPlanner._anchor("ls -la", s), 'cd "/tmp/ws_1" && ls -la'
        )

    def test_anchor_keeps_existing_cd(self):
        s = TaskSession(task_dir="/tmp/ws_1")
        cmd = 'cd "/tmp/ws_1" && ./check'
        self.assertEqual(TaskPlanner._anchor(cmd, s), cmd)

    def test_anchor_keeps_absolute(self):
        s = TaskSession(task_dir="/tmp/ws_1")
        cmd = "/usr/bin/python3 foo.py"
        self.assertEqual(TaskPlanner._anchor(cmd, s), cmd)

    def test_anchor_chained_cd(self):
        s = TaskSession(task_dir="/tmp/ws_1")
        cmd = "ls && cd sub && cat f"
        self.assertEqual(TaskPlanner._anchor(cmd, s), cmd)


if __name__ == "__main__":
    unittest.main()
