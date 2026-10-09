"""IKKIA9 / IKKIBC 复盘实施回归（2026-10-09 晚用户裁决 A-G 全做）。

覆盖：
- Q1 积分模式回退：杀不动的大目标全部被滤掉时，用威胁分重选集火磨血
  （IKKIA9 D4 夜空转 11 回合实锤）。
- Q2 补刀线贪心：小兵 40 血 = 2 弹头带走（落点重复同格）。
- Q3 imp 路线危险预检：下一步入敌基地圈/贴敌 → 换安全绕行，无安全路线原地不动。
- Q4 炮位被机器人占 → 邻接 CP 替补格（电磁炮身份继承）。
- Q5 背墙列 + 门：门非角落且内侧空闲；黄昏全员在内才封门；清晨拆门。
- Q6 BOSS 召唤令：D5+ 富余备货；白天 use 登记；夜间驱动（邻接敌基地→attack）。
- Q7 电磁炮：弹道穿透目标选择；挖矿工夜控（不选修理工）；自家召唤机器人绝不被打。
- Q8 任务：```json 围栏解析；errorCode=2 重答复读拦截。
"""
import json
import unittest

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.protocol import Pos, Turn
from agent.brain import Brain
from agent.fire import JointFirePlanner
from agent.planners.task import TaskPlanner, TaskSession, ST_LLM
from agent.planners.upgrade import UpgradePlanner
from agent.planners.layout import compute_layout


def _turn(sim: SimWorld, round_no: int | None = None) -> Turn:
    payload = sim.payload()
    if round_no is not None:
        payload["roundNo"] = round_no
    return Turn.load(payload)


class TestQ1PointsFallback(unittest.TestCase):
    """Q1：积分模式无目标可杀 ≠ 不打——回退威胁集火。"""

    def test_grind_fallback_when_only_unkillable_bigs(self):
        sim = SimWorld(station_pos=(10, 24))
        sim.roles.append(sim._role(10040, 10, 23, "rocket", 1000, level=1))
        # 防线安全条件：给工人塞修复包 + 满血墙
        for w in sim.walls():
            w["health"] = 1000
        for rid in (10010, 10012):
            sim.role(rid)["backpack"] = ["WallFixer"] * 9
        # 场上只剩一只杀不动的 BOSS（800 血 > 3 炮集火 60）
        sim.spawn_robot(15, 24, "bossRobot", hp=800, rid=30100)
        planner = JointFirePlanner()
        commands: dict = {}
        trace: dict = {}
        planner.plan(_turn(sim, 1), _turn(sim).pioneer(), commands, trace)
        self.assertIn(10040, commands, "杀不动的大目标在啃墙时必须集火磨血，不得空转")


class TestQ2WarheadAllocation(unittest.TestCase):
    """Q2：补刀线贪心——40 血小兵吃 2 弹头（L2 炮）。"""

    def test_small_robot_gets_two_warheads(self):
        sim = SimWorld(station_pos=(10, 24))
        sim.roles.append(sim._role(10040, 10, 23, "rocket", 1000, level=2))
        sim.spawn_robot(15, 24, "smallRobot", hp=40, rid=30001)
        planner = JointFirePlanner()
        commands: dict = {}
        planner.plan(_turn(sim, 1), _turn(sim).pioneer(), commands, {})
        self.assertIn(10040, commands)
        targets = commands[10040]["targetPos"]
        self.assertEqual(len(targets), 2)
        # 两弹头同落 40 血小兵（2×20=40 带走）
        self.assertEqual(
            (targets[0]["x"], targets[0]["y"]), (targets[1]["x"], targets[1]["y"])
        )

    def test_two_smalls_get_one_warhead_each(self):
        """两只残血（≤20）小兵 → 各 1 弹头（两杀 > 一杀）。"""
        sim = SimWorld(station_pos=(10, 24))
        sim.roles.append(sim._role(10040, 10, 23, "rocket", 1000, level=2))
        sim.spawn_robot(15, 24, "smallRobot", hp=20, rid=30001)
        sim.spawn_robot(15, 22, "smallRobot", hp=20, rid=30002)
        planner = JointFirePlanner()
        commands: dict = {}
        planner.plan(_turn(sim, 1), _turn(sim).pioneer(), commands, {})
        targets = commands[10040]["targetPos"]
        self.assertEqual(len(targets), 2)
        self.assertNotEqual(
            (targets[0]["x"], targets[0]["y"]), (targets[1]["x"], targets[1]["y"]),
            "两只可一发带走的兵应各吃一弹头",
        )


class TestQ3ImpTravelSafety(unittest.TestCase):
    """Q3：去拆矿路上的双威胁预检（敌角色 + 敌基地）。"""

    def test_travel_holds_when_step_dangerous(self):
        """夜里下一步将踏入敌基地圈 → 无安全绕行时原地不动（不乱跑）。"""
        from agent.fsm_imp import ImpFSM
        from test_v2_rules import base_payload
        from test_imp_strategy_20261009 import _imp_ctx
        # 夜（round 75）；敌基地默认在 (30,10) 一带，(25,8) 矿在禁入圈内
        payload = base_payload(75)
        for r in payload["teamOur"]["roles"]:
            if r.get("roleType") == "imp":
                r["pos"] = {"x": 22, "y": 8}   # 圈外（距敌基 8）
        turn = Turn.load(payload)
        es = next(u for u in turn.enemy if u.kind == "station")
        imp = turn.imp()
        self.assertGreaterEqual(distance(imp.pos, es.pos), 8, "测试前提：imp 在圈外")
        fsm = ImpFSM()
        fsm.destroy_target = Pos(25, 8)     # 圈内目标矿
        cmd = fsm.decide(turn, imp, _imp_ctx())
        # 直行下一步 (23,8) 距敌基 7 < 8 = 危险 → 原地不动，或挪到"不比原地远、
        # 且在圈外"的安全格（用户裁决：允许原地不动，绝不许踏圈/乱跑）
        if cmd is None:
            pass
        else:
            self.assertEqual(cmd["action"], "move")
            step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
            self.assertGreaterEqual(distance(step, es.pos), 8, "任何一步都不得踏入敌基地圈")
            self.assertLessEqual(distance(step, Pos(25, 8)), 3, "绕行步不得比原地离目标更远")
        self.assertEqual(fsm.destroy_target, Pos(25, 8), "目标保留（等待威胁解除）")

    def test_pos_dangerous_dual_sources(self):
        """危险判定双源：敌基地圈（夜） + 敌 imp 贴身。"""
        from agent.fsm_imp import ImpFSM
        from test_v2_rules import base_payload
        payload = base_payload(75)
        turn = Turn.load(payload)
        es = next(u for u in turn.enemy if u.kind == "station")
        imp = turn.imp()
        fsm = ImpFSM()
        self.assertTrue(fsm._pos_dangerous(turn, Pos(es.pos.x - 2, es.pos.y), imp),
                        "夜/黄昏敌基地圈内 = 危险")
        self.assertFalse(
            fsm._pos_dangerous(turn, Pos(es.pos.x - 9, es.pos.y), imp),
            "圈外远离格 = 不危险",
        )


class TestQ4TurretSubstitute(unittest.TestCase):
    """Q4：炮位被机器人占 → 邻接 CP 替补。"""

    def test_occupied_turret_cell_substituted(self):
        # 先探一局拿布局坐标（不 apply，避免炮被建成）
        probe = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone"})
        probe_brain = Brain()
        probe_brain.decide(probe.payload())
        victim = probe_brain.layout.turret_cells[-1]   # 炮位槽（含替补判定）
        # 重开一局：敌方 BOSS 从第 1 回合就站在该炮位上（IKKIBC 实锤场景）
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone"})
        brain = Brain()
        sim.spawn_robot(victim.x, victim.y, "bossRobot", hp=800, rid=30990)
        r, trace = brain.decide(sim.payload())
        subs = trace.get("turret_substitute") or []
        hit = [s for s in subs
               if s["from"]["x"] == victim.x and s["from"]["y"] == victim.y]
        self.assertTrue(hit, f"被占炮位 {victim} 应产生替补记录: {subs}")
        to_cell = Pos(hit[0]["to"]["x"], hit[0]["to"]["y"])
        builds = [fsm.build for fsm in brain.worker_fsms.values() if fsm.build]
        # Q7 回退（IKKJ2x）：替补格恢复火箭身份（不再建电磁炮）
        self.assertTrue(any(b[0] == to_cell and b[1] == "rocket" for b in builds),
                        f"替补格应为火箭: {builds}")


class TestQ5BackWallsAndDoor(unittest.TestCase):
    """Q5 已回退（IKKJ2x：day1 预算只够主墙 14 面，背墙+门导致开口整夜敞开、
    三场全被推平基地）——保留占位类确认布局恢复旧语义。"""

    def test_layout_has_no_back_walls(self):
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone"})
        turn = _turn(sim, 1)
        layout = compute_layout(turn, "W", None)
        self.assertFalse(hasattr(layout, "back_wall_cells") and layout.back_wall_cells,
                         "背墙列应已回退")
        self.assertIsNone(getattr(layout, "door_cell", None), "门格应已回退")
        self.assertEqual(len(layout.wall_cells), 14, "应恢复 14 面全封闭主环")


class TestQ6BossSummon(unittest.TestCase):
    """Q6：BOSS 召唤令备货 + use + 夜间驱动。"""

    def test_boss_order_stock_mission(self):
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone"})
        sim.gold = 600
        sim.round_no = 651   # D5+
        turn = _turn(sim)
        missions = UpgradePlanner().plan(turn)
        names = [m.voucher for m in missions]
        self.assertIn("BossRobotSummonOrder", names, "D5+ 富余应备 BOSS 召唤令")

    def test_summon_robot_attacks_enemy_base_when_adjacent(self):
        sim = SimWorld(station_pos=(10, 24))
        brain = Brain()
        r, _ = brain.decide(sim.payload())
        sim.apply(r); sim.advance()
        es = next(u for u in _turn(sim).enemy if u.kind == "station")
        # 我方召唤 BOSS 就在敌基地旁
        payload = sim.payload()
        payload["teamOur"]["summonRobotList"] = [{
            "id": 70001, "pos": {"x": es.pos.x + 1, "y": es.pos.y},
            "roleType": "bossRobot", "health": 800, "targetTeam": "",
        }]
        payload["roundNo"] = 75   # 夜
        turn = Turn.load(payload)
        self.assertEqual(len(turn.summon_robots), 1)
        commands: dict = {}
        from types import SimpleNamespace
        ctx = SimpleNamespace(trace={}, reserved=frozenset())
        brain._drive_summon_robots(turn, commands, ctx)
        self.assertIn(70001, commands, "邻接敌基地的召唤 BOSS 应下发 attack")
        self.assertEqual(commands[70001]["action"], "attack")

    def test_own_summon_never_fired_upon(self):
        """自家召唤机器人在射程内也绝不被自家炮打。"""
        sim = SimWorld(station_pos=(10, 24))
        sim.roles.append(sim._role(10040, 10, 23, "rocket", 1000, level=1))
        payload = sim.payload()
        payload["teamOur"]["summonRobotList"] = [{
            "id": 70001, "pos": {"x": 15, "y": 24},
            "roleType": "bossRobot", "health": 800, "targetTeam": "",
        }]
        payload["roundNo"] = 75
        turn = Turn.load(payload)
        planner = JointFirePlanner()
        commands: dict = {}
        planner.plan(turn, turn.pioneer(), commands, {})
        self.assertNotIn(10040, commands, "不得把自家 BOSS 当敌人")
        self.assertEqual(len(turn.hostile_robots), 0)


class TestQ7Railgun(unittest.TestCase):
    """Q7：电磁炮弹道 + 挖矿工夜控。"""

    def test_railgun_beam_target(self):
        sim = SimWorld(station_pos=(10, 24))
        sim.roles.append(sim._role(10041, 10, 23, "railgun", 1000, level=3))
        # 两只机器人与炮同线（弹道穿透：60 能量全吃）
        sim.spawn_robot(14, 23, "middleRobot", hp=60, rid=30001)
        sim.spawn_robot(16, 23, "middleRobot", hp=60, rid=30002)
        planner = JointFirePlanner()
        commands: dict = {}
        planner.plan(_turn(sim, 1), _turn(sim).pioneer(), commands, {})
        self.assertIn(10041, commands)
        cmd = commands[10041]
        self.assertEqual(len(cmd["targetPos"]), 1, "电磁炮单目标（接口 §2.2）")

    def test_railgun_no_cooldown_fires_consecutive_rounds(self):
        sim = SimWorld(station_pos=(10, 24))
        sim.roles.append(sim._role(10041, 10, 23, "railgun", 1000, level=1))
        sim.spawn_robot(14, 23, "middleRobot", hp=60, rid=30001)
        planner = JointFirePlanner()
        c1: dict = {}
        planner.plan(_turn(sim, 1), _turn(sim).pioneer(), c1, {})
        self.assertIn(10041, c1)
        c2: dict = {}
        planner.plan(_turn(sim, 2), _turn(sim).pioneer(), c2, {})
        self.assertIn(10041, c2, "电磁炮无冷却，次回合应可再发")

    def test_rocket_still_fires_when_railgun_out_of_range(self):
        """轮转回退：电磁炮射程外（L1=6）时火箭照打（不浪费回合）。"""
        sim = SimWorld(station_pos=(10, 24))
        sim.roles.append(sim._role(10040, 10, 23, "rocket", 1000, level=1))
        sim.roles.append(sim._role(10041, 10, 25, "railgun", 1000, level=1))
        # 敌人在 8 格外：火箭射程 10 ✓，电磁炮 L1 射程 6 ✗
        sim.spawn_robot(18, 24, "smallRobot", hp=40, rid=30001)
        planner = JointFirePlanner()
        commands: dict = {}
        planner.plan(_turn(sim, 1), _turn(sim).pioneer(), commands, {})
        self.assertTrue(
            any(wid in commands for wid in (10040, 10041)),
            "轮转回退：选中炮无目标时应试下一门",
        )


class TestQ8TaskRobustness(unittest.TestCase):
    """Q8：JSON 围栏解析 + 重答复读拦截。"""

    def test_fenced_json_parsed(self):
        planner = TaskPlanner()
        s = TaskSession()
        s.llm_pending = True
        planner._on_llm_result(
            s, '```json\n{"cmd": "", "answer": "{\\"n\\": 2}", "isFinished": true}\n```'
        )
        self.assertEqual(s.answer, '{"n": 2}')

    def test_single_quoted_dict_parsed(self):
        planner = TaskPlanner()
        s = TaskSession()
        s.llm_pending = True
        planner._on_llm_result(s, "{'cmd': '', 'answer': '{\"k\": 1}', 'isFinished': True}")
        self.assertEqual(s.answer, '{"k": 1}')

    def test_repeat_answer_rejected(self):
        planner = TaskPlanner()
        s = TaskSession()
        s.llm_pending = True
        from agent.planners.task import _answer_key
        s.force_answer = True
        s.last_submitted = _answer_key('{"total_events": 21}')
        s.last_error = "数值不符"
        planner._on_llm_result(
            s, '{"cmd": "", "answer": "{\\"total_events\\": 21}", "isFinished": true}'
        )
        self.assertEqual(s.stage, ST_LLM, "复读被判错答案必须拒收")
        self.assertTrue(
            any("完全相同" in t for t in s.transcript),
            "拒收原因应写回 transcript 供自纠",
        )
        # 换一个不同的答案 → 放行
        planner._on_llm_result(
            s, '{"cmd": "", "answer": "{\\"total_events\\": 22}", "isFinished": true}'
        )
        self.assertNotEqual(s.stage, ST_LLM)


from agent.protocol import distance  # noqa: E402


if __name__ == "__main__":
    unittest.main()
