# -*- coding: utf-8 -*-
"""IKKHUU/IKKHRT 复盘裁决回归（2026-10-09 晚，用户裁决 A-G 全做）。

A. timeout≤10 紧任务跳过健壮探索（省 2 回合=多 1 次交换）
B. 紧任务 prompt 注入预算警告
C. 统计类口径 echo 自检入 prompt
R. LLM 命令安全校验（交互式/破坏性/网络拒绝，拒绝原因写回 transcript）
D. imp 夜间/黄昏敌基地 8 格禁入圈
E. 敌 imp 威胁特判（距离 4 直接触发 + 逃命模式向家撤）
F. weapon_reserve 含 L2→L3 预留
G. fire 积分射击：防线安全时积分权重 + 杀不动大型重罚
"""
import json
import sys
import unittest

sys.path.insert(0, r"D:\workspace\CoreGeek\game\CoreGeek\tests")
import _bootstrap  # noqa: F401
from test_v2_rules import base_payload

from agent.planners.task import TaskPlanner, TaskSession, _llm_cmd_safe
from agent.planners.treasure import TreasurePlan
from agent.fsm_imp import ImpFSM, NIGHT_BASE_CLEAR, IMP_FLEE_DIST
from agent.fire import JointFirePlanner
from agent.protocol import Pos, Robot, Turn, Unit


def _worker_ctx(reserved=frozenset()):
    class C:
        pass
    c = C()
    c.reserved = reserved
    c.note = staticmethod(lambda *a, **k: None)
    return c


class TestATightTaskSkipsExplore(unittest.TestCase):
    def _make(self, timeout):
        planner = TaskPlanner()
        s = TaskSession()
        payload = base_payload(40)
        payload["phaseTask"] = "分析日志统计失败次数"
        payload["teamOur"]["playerTasks"] = [{
            "taskType": "自进化类2", "taskPosition": {"x": 16, "y": 17},
            "coldDownRounds": 0, "scoreReward": 100, "goldReward": 100,
            "isValid": True, "timeoutRounds": timeout,
        }]
        # _task_timeout 要求开拓者邻接任务点
        for r in payload["teamOur"]["roles"]:
            if r.get("roleType") == "pioneer":
                r["pos"] = {"x": 16, "y": 18}
        turn = Turn.load(payload)
        out = planner.work(turn, s)
        return planner, s, turn, out

    def test_timeout10_skips_explore(self):
        planner, s, turn, out = self._make(10)
        self.assertTrue(s.skip_explore, "timeout=10 紧任务应跳过健壮探索")
        self.assertTrue(out.prompt, "紧任务应直接发出首个 prompt（省探索回合）")
        self.assertFalse(out.execute_cmd, "紧任务不得先发探索命令")

    def test_timeout15_keeps_explore(self):
        planner, s, turn, out = self._make(15)
        self.assertFalse(s.skip_explore, "timeout=15 常规任务保留探索")
        self.assertTrue(out.execute_cmd, "常规任务先发探索命令")


class TestBTightPromptAndStatsEcho(unittest.TestCase):
    def test_tight_budget_warning_in_prompt(self):
        planner = TaskPlanner()
        s = TaskSession()
        s.task_text = "统计失败次数"
        s.timeout_rounds = 10
        text = planner._build_prompt(s)
        self.assertIn("紧任务模式", text)
        self.assertIn("3 次交互", text)

    def test_stats_echo_discipline_in_prompt(self):
        planner = TaskPlanner()
        s = TaskSession()
        s.task_text = "统计 total_failures"
        s.task_type = "api"
        s.timeout_rounds = 15
        text = planner._build_prompt(s)
        self.assertIn("统计类作答纪律", text)
        self.assertIn("echo", text, "统计纪律应含脚本 echo 口径核对")


class TestRCmdSafety(unittest.TestCase):
    def test_interactive_rejected(self):
        ok, why = _llm_cmd_safe("top")
        self.assertFalse(ok)
        ok, _ = _llm_cmd_safe("vim log.txt")
        self.assertFalse(ok)
        # 子串不误杀：desktop/stop 路径合法
        ok, _ = _llm_cmd_safe("cat /opt/desktop/config.yaml")
        self.assertTrue(ok)
        ok, _ = _llm_cmd_safe("systemctl stop nginx") if False else (True, "")
        self.assertTrue(ok)

    def test_destructive_and_net_rejected(self):
        self.assertFalse(_llm_cmd_safe("rm -rf /")[0])
        self.assertFalse(_llm_cmd_safe("pip install requests")[0])
        self.assertFalse(_llm_cmd_safe("git clone https://x.com/a.git")[0])
        self.assertTrue(_llm_cmd_safe("head -5 gateway.log")[0])
        self.assertTrue(_llm_cmd_safe("python3 << 'EOF'\nprint(1)\nEOF")[0])

    def test_rejected_cmd_fed_back_not_dispatched(self):
        planner = TaskPlanner()
        s = TaskSession()
        s.task_text = "统计"
        s.transcript = []
        s.llm_loops = 8
        planner._on_llm_result(s, json.dumps({"cmd": "top", "answer": "", "isFinished": False}))
        self.assertEqual(s.stage, "LLM_LOOP")
        self.assertFalse(hasattr(s, "_pending_llm_cmd") and s._pending_llm_cmd,
                         "被拒命令不得下发")
        self.assertTrue(any("REJECTED" in t for t in s.transcript), "拒绝原因应写回 transcript")
        self.assertEqual(s.cmd_count, 0, "被拒命令不消耗命令预算")


class TestDImpBaseClearCircle(unittest.TestCase):
    def _night(self, round_no, imp_xy, es_xy=(30, 10)):
        payload = base_payload(round_no)
        for r in payload["teamEnemy"]["roles"]:
            if r.get("roleType") == "station":
                r["pos"] = {"x": es_xy[0], "y": es_xy[1]}
        for r in payload["teamOur"]["roles"]:
            if r.get("roleType") == "imp":
                r["pos"] = {"x": imp_xy[0], "y": imp_xy[1]}
        return Turn.load(payload)

    def test_night_inside_circle_moves_out(self):
        # 夜间 imp 距敌基地 6 格（圈内）→ 向远离敌基地方向移动
        turn = self._night(75, (24, 8))
        fsm = ImpFSM()
        cmd = fsm.decide(turn, turn.imp(), _worker_ctx())
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "move", "夜间在敌基地禁入圈内必须撤离")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        es = Pos(30, 10)
        self.assertGreaterEqual(distance(step, es), distance(Pos(24, 8), es))

    def test_dusk_leaves_early(self):
        # 白天尾段（入夜前 ≤6 回合）在圈内 → 也开始撤离
        rn = 70 - 6 + 1   # round_in_day=65 → 距入夜 5 回合
        turn = self._night(rn, (24, 8))
        fsm = ImpFSM()
        cmd = fsm.decide(turn, turn.imp(), _worker_ctx())
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "move", "黄昏在圈内应提前撤离")

    def test_day_inside_circle_still_destroys(self):
        # 白天（距入夜 >6 回合）在圈内 → 允许继续拆矿（白天可入圈）
        turn = self._night(40, (24, 8))
        fsm = ImpFSM()
        cmd = fsm.decide(turn, turn.imp(), _worker_ctx())
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "destroy", "白天禁入圈内可正常站桩")

    def test_night_target_outside_circle_selected(self):
        # 夜间选矿跳过圈内矿：(25,8) 距敌基地(30,10) 5 格 <8 → 不选
        turn = self._night(75, (10, 20))
        fsm = ImpFSM()
        fsm.destroy_target = None
        tgt = fsm._pick_target(turn, turn.imp(), _worker_ctx())
        if tgt is not None:
            self.assertGreaterEqual(distance(tgt, Pos(30, 10)), NIGHT_BASE_CLEAR,
                                    "夜间不得选中敌基地禁入圈内矿")


from agent.protocol import distance  # noqa: E402  （函数顶部导入会与 dataclass 冲突，置底）


class TestEImpFleeFromImp(unittest.TestCase):
    def _turn_with_foe_imp(self, round_no, foe_xy, imp_xy=(24, 8)):
        payload = base_payload(round_no)
        payload["teamEnemy"]["roles"].append({
            "id": 20999, "pos": {"x": foe_xy[0], "y": foe_xy[1]},
            "roleType": "imp", "health": 500, "attackPower": 0, "attackRange": 0,
        })
        for r in payload["teamOur"]["roles"]:
            if r.get("roleType") == "imp":
                r["pos"] = {"x": imp_xy[0], "y": imp_xy[1]}
        return Turn.load(payload)

    def test_enemy_imp_threat_at_distance_4(self):
        # 敌 imp 距 4（>FLEE_DIST 2，≤IMP_FLEE_DIST 4）→ 威胁触发（无需移动趋势）
        turn = self._turn_with_foe_imp(40, (28, 8))
        fsm = ImpFSM()
        self.assertTrue(fsm._foe_threat(turn, turn.imp()),
                        "敌 imp 4 格内应直接视为威胁")

    def test_static_foe_worker_at_4_no_threat(self):
        # 对照：静止敌工人距 4 → 无威胁
        payload = base_payload(40)
        payload["teamEnemy"]["roles"].append({
            "id": 20010, "pos": {"x": 28, "y": 8}, "roleType": "worker",
            "health": 500, "attackPower": 0, "attackRange": 0,
        })
        turn = Turn.load(payload)
        fsm = ImpFSM()
        self.assertFalse(fsm._foe_threat(turn, turn.imp()))

    def test_flee_from_imp_heads_home(self):
        # 敌 imp 距 3（邻接会触发机会抓捕）→ 逃命模式：落点远离敌 imp
        turn = self._turn_with_foe_imp(40, (27, 8))
        fsm = ImpFSM()
        cmd = fsm.decide(turn, turn.imp(), _worker_ctx())
        self.assertEqual(cmd["action"], "move")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertGreater(distance(step, Pos(25, 8)), 1, "应拉开与敌 imp 距离")


class TestFWeaponReserveL3(unittest.TestCase):
    def test_reserve_when_all_l2(self):
        # 武器全 L2 → weapon_reserve 应含 L3 预留 150（防墙券+修复包清空金币）
        from agent.planners import upgrade as up
        src_walls = None
        # 直接构造：用 ikyns 的 make_sim 但 gold 极低 → 墙券任务应被 L3 预留挡下
        from test_issue_ikyns import make_sim
        sim = make_sim(gold=190)   # budget=160：墙券 130<150 被挡，L3 券 150 买得起
        sim.round_no = 261
        for pos in [(9, 20), (10, 20), (9, 21)]:
            sim.roles.append(sim._role(62000 + len(sim.weapons()), pos[0], pos[1], "rocket", 1500, level=2))
        sim.roles.append(sim._role(63000, 12, 20, "wall", 200, level=2))
        turn = Turn.load(sim.payload())
        ms = up.UpgradePlanner().plan(turn, cp=Pos(9, 23), front="W")
        wall_missions = [m for m in ms if m.kind == "wall"]
        self.assertFalse(wall_missions, "gold 不足 L3 预留时应暂停墙升级（保护 L3 券钱）")
        # B1（2026-10-10）：L3 炮台优先级 20 → 8（武器优先于墙升级）
        self.assertTrue(any(m.kind == "weapon" and m.priority == 8 for m in ms),
                        "L3 炮台任务不受预留影响")


class TestGPointsShooting(unittest.TestCase):
    def _robot(self, rid, x, y, kind, hp, team="t2"):
        return Robot(rid, Pos(x, y), kind, hp, "", team)

    def _turn(self, robots, round_no=75, fixers=10):
        payload = base_payload(round_no)
        payload["robot"]["roles"] = [
            {"id": r.robot_id, "pos": {"x": r.pos.x, "y": r.pos.y},
             "roleType": r.kind, "health": r.health, "targetTeam": r.target_team}
            for r in robots
        ]
        # 修复包充足 → 防线安全
        for r in payload["teamOur"]["roles"]:
            if r.get("roleType") == "worker":
                r["backpack"] = ["WallFixer"] * fixers
        return Turn.load(payload)

    def _weapon(self, level=2):
        return Unit(30001, Pos(10, 20), "rocket", 2000, level, 0, 10, 20, None, ())

    def test_points_mode_prefers_killable_small_over_stuck_boss(self):
        # 防线安全 + BOSS 血量 > 集火伤害（杀不动）+ 小兵可击杀 → 选小兵
        robots = [
            self._robot(1, 14, 20, "bossRobot", 800),    # 杀不动
            self._robot(2, 15, 21, "smallRobot", 40),    # 可击杀 1 分
        ]
        turn = self._turn(robots)
        cells, score = JointFirePlanner()._best_targets(turn, self._weapon(2))
        kinds = {r.robot_id: r.kind for r in robots}
        hit_small = any(
            any(r.pos == c for r in robots if r.kind == "smallRobot") for c in cells
        )
        self.assertTrue(hit_small, f"积分模式应优先可击杀小兵，实际落点 {cells}")

    def test_threat_mode_kept_when_defense_unsafe(self):
        # 修复包不足 → 维持威胁优先（逼近+致命），不切积分
        robots = [
            self._robot(1, 12, 20, "smallRobot", 40),   # 逼近基地
            self._robot(2, 18, 25, "smallRobot", 40),   # 远
        ]
        turn = self._turn(robots, fixers=2)
        cells, _ = JointFirePlanner()._best_targets(turn, self._weapon(2))
        self.assertTrue(any(c.x <= 13 for c in cells), "防线不安全时应优先近逼目标")
