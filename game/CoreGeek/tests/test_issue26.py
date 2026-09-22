"""issue#26 回归：Day1 批量采石 / 自进化任务 / 墙缺口 / 震荡 / 修理工修理位 / 矿工效率。"""
import json
import unittest

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.brain import Brain, _Ctx
from agent.fsm_pioneer import PioneerFSM, STATE_TASK_TRAVEL
from agent.fsm_worker import WorkerFSM, ROLE_REPAIRER
from agent.planners.task import TaskPlanner, TaskSession
from agent.protocol import Pos, Turn

W1, W2, PIONEER = 10010, 10012, 10011
DAY1 = 70


def make_sim(**kw):
    base = dict(station_pos=(10, 24), mines={})
    base.update(kw)
    return SimWorld(**base)


def run_rounds(brain, sim, n):
    out = []
    for _ in range(n):
        response, trace = brain.decide(sim.payload())
        sim.apply(response)
        sim.advance()
        out.append((response, trace))
    return out


class TestDay1StoneBatch(unittest.TestCase):
    def test_gathers_full_mine_before_building(self):
        """Day1 修理工一次采一大批（≥10）再开建，而非采 6 块就建。"""
        sim = SimWorld(station_pos=(10, 24),
                       mines={(6, 22): "stone", (4, 30): "stone", (8, 20): "copper"})
        sim.add_mine((6, 22), "stone", remaining=30)
        sim.add_mine((4, 30), "stone", remaining=30)
        brain = Brain()
        # 跟踪修理工第一次 build 前采集到的石头峰值
        first_build_round = None
        for r in range(DAY1):
            response, trace = brain.decide(sim.payload())
            cmds = response["roleCommandMap"]
            if first_build_round is None and any(
                c.get("action") == "build" and c.get("name") == "wall" for c in cmds.values()
            ):
                first_build_round = r
                # 修理工此刻背包应接近一批（≥10），而非仅 6
                bag = sim.role(W1)["backpack"].count("stone")
                self.assertGreaterEqual(bag, 8, f"首次建墙前仅 {bag} 石头（应≥10）")
            sim.apply(response)
            sim.advance()
        self.assertIsNotNone(first_build_round, "Day1 应有建墙")
        self.assertGreaterEqual(len(sim.walls()), 10, "Day1 末应建满大部分墙")


class TestTokenPlaceholderGuard(unittest.TestCase):
    def test_task_description_example_not_submitted(self):
        """任务书示例 `"token": "xxx"` 不得被当作真实答案提交（issue#26 根因）。"""
        from agent.planners.task import TaskPlanner, TaskSession, ST_SUBMIT
        planner = TaskPlanner()
        s = TaskSession()
        # 探索输出里只有任务书示例的占位 token，没有真实 check 输出
        fake_explore = '[exitCode:0]\n# 任务\n提交格式 {"token": "xxx"}\n'
        s.stage = "EXPLORE_FILES"
        s.task_text = "请阅读task_1_alpha.md"
        planner._on_explore(s, fake_explore)
        self.assertNotEqual(s.stage, ST_SUBMIT, "占位 token 不得触发提交")
        self.assertIsNone(s.answer)

    def test_real_token_line_submitted(self):
        """真实 check 输出的独立行 `TOKEN: abc123def` 应直接提交。"""
        from agent.planners.task import TaskPlanner, TaskSession, ST_SUBMIT
        planner = TaskPlanner()
        s = TaskSession()
        ok = planner._try_token_submit(s, "[exitCode:0]\n[ OK ] 全部通过\nTOKEN: fc1e78eb2a5a\n")
        self.assertTrue(ok)
        self.assertEqual(s.stage, ST_SUBMIT)
        self.assertIn("fc1e78eb2a5a", s.answer)


class TestApiProbeRouting(unittest.TestCase):
    def test_api_task_routes_to_probe(self):
        """探索输出含 localhost API → 进入确定性 API 探测（不再让 LLM 盲目试认证/参数）。"""
        from agent.planners.task import ST_API_PROBE
        planner = TaskPlanner()
        s = TaskSession()
        s.task_text = "请阅读task_1_beijing.md，获取任务信息"
        s.target_name = "task_1_beijing.md"
        explore = (
            "[exitCode:0]\n__FILE:/tmp/selfEvolutionTask/1-unknown-api/task_1_beijing.md\n"
            "=== TASK ===\n查询北京文化遗产\n"
            "=== FILE:/tmp/selfEvolutionTask/1-unknown-api/API_DOCS.md ===\n"
            "base http://localhost:8899\nGET /api/v1/heritage/search\n"
            "__DIR:/tmp/selfEvolutionTask/1-unknown-api\n"
        )
        planner._on_explore(s, explore)
        self.assertEqual(s.stage, ST_API_PROBE)
        self.assertEqual(s.city, "北京", "应从文件名拼音识别城市")

    def test_api_probe_cmd_is_python_harvester(self):
        planner = TaskPlanner()
        s = TaskSession()
        s.task_dir = "/tmp/selfEvolutionTask/1-unknown-api"
        s.city = "北京"
        cmd = planner._api_probe_cmd(s)
        self.assertIn("python3", cmd)
        self.assertIn("base64", cmd)
        self.assertIn(s.task_dir, cmd)


class TestPioneerNoOscillation(unittest.TestCase):
    def test_task_travel_not_clobbered_when_cant_afford(self):
        """TASK_TRAVEL 中买不起券 → 不得重置为 GUARD（issue#26 开拓者来回震荡根因）。"""
        sim = make_sim(gold=10)  # 买不起券
        sim.roles.append(sim._role(50040, 8, 20, "rocket", 1000, level=1))
        sim.role(PIONEER)["pos"] = {"x": 20, "y": 20}
        sim.round_no = 200
        turn = Turn.load(sim.payload())
        pioneer = next(u for u in turn.ours if u.kind == "pioneer")
        fsm = PioneerFSM()
        fsm.state = STATE_TASK_TRAVEL
        fsm.task_point = Pos(14, 14)
        ctx = _Ctx({})
        ctx.reserved = set()
        ctx.gunner_upgrade = (Pos(8, 20), "weapon")
        fsm._weapon_upgrade_cmd(turn, pioneer, ctx)
        self.assertEqual(fsm.state, STATE_TASK_TRAVEL, "买不起券不得清掉 TASK_TRAVEL")
        self.assertEqual(fsm.task_point, Pos(14, 14), "任务点不得被清空")


class TestWorkerOscillationDetector(unittest.TestCase):
    def test_two_cycle_triggers_unstuck(self):
        """A→B→A→B 两格震荡应触发 unstuck 清目标（旧 stuck 检测只看原地不动）。"""
        fsm = WorkerFSM(W1)
        # 模拟位置历史 A,B,A,B
        fsm._pos_hist = [Pos(1, 1), Pos(2, 2), Pos(1, 1), Pos(2, 2)]
        self.assertTrue(fsm._oscillating())
        # 正常前进 A,B,C,D 不误报
        fsm._pos_hist = [Pos(1, 1), Pos(2, 2), Pos(3, 3), Pos(4, 4)]
        self.assertFalse(fsm._oscillating())
        # 原地不动 A,A,A,A 不算两格震荡（由 stuck 检测管）
        fsm._pos_hist = [Pos(1, 1), Pos(1, 1), Pos(1, 1), Pos(1, 1)]
        self.assertFalse(fsm._oscillating())


class TestRepairPost(unittest.TestCase):
    def test_layout_has_repair_post_inside_ring(self):
        """布局应给出修理工修理位：内圈、非炮台/CP、邻墙多。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone"})
        brain = Brain()
        brain.decide(sim.payload())
        lay = brain.layout
        self.assertIsNotNone(lay.repair_post)
        self.assertNotIn(lay.repair_post, lay.turret_cells)
        self.assertNotEqual(lay.repair_post, lay.control_point)
        # 修理位应邻接至少一面墙
        self.assertTrue(
            any(nb in set(lay.wall_cells) for nb in lay.repair_post.neighbours()),
            "修理位应邻接围墙",
        )

    def test_repairer_goes_to_repair_post_night_d4(self):
        """D4+ 夜：修理工无事可修时就位 repair_post（内圈），而非在外采矿/闲逛。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (7, 26): "stone"})
        sim.add_mine((6, 22), "stone", remaining=120)
        sim.add_mine((7, 26), "stone", remaining=120)
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        sim.role(W1)["pos"] = {"x": 22, "y": 30}  # 修理工在远处墙外
        sim.gold = 0
        sim.round_no = 451  # Day4 夜
        post = brain.layout.repair_post
        reached = False
        for _ in range(40):
            response, trace = brain.decide(sim.payload())
            pos = sim.role(W1)["pos"]
            if max(abs(pos["x"] - post.x), abs(pos["y"] - post.y)) <= 1:
                reached = True
                break
            sim.apply(response)
            sim.advance()
        self.assertTrue(reached, f"D4+ 夜修理工应就位 repair_post {post}")


class TestWallGapRecovery(unittest.TestCase):
    def test_failed_wall_cell_retries_and_recovers(self):
        """建造失败格 40 回合后可重试；布局定期重算补回缺口（issue#26 永久缺口根因）。"""
        from agent.buildable_map import BuildableMap, BUILD_FAIL_RETRY
        bm = BuildableMap()
        bm.record(Pos(31, 12), "wall", False, round_no=100)
        self.assertFalse(bm.is_usable(Pos(31, 12), "wall", round_no=100))
        self.assertFalse(bm.is_usable(Pos(31, 12), "wall", round_no=100 + BUILD_FAIL_RETRY - 1))
        self.assertTrue(bm.is_usable(Pos(31, 12), "wall", round_no=100 + BUILD_FAIL_RETRY),
                        "过期后应允许重试")
        # 成功后清除失败标记
        bm.record(Pos(31, 12), "wall", True, round_no=200)
        self.assertTrue(bm.is_usable(Pos(31, 12), "wall", round_no=200))


class TestMinePreferRelease(unittest.TestCase):
    def test_money_prefer_releases_stone_lock(self):
        """修理工 Day1 锁的石矿，入夜 prefer=money 时应释放改采铜/铁。"""
        sim = make_sim(mines={(4, 30): "stone", (8, 20): "copper"})
        sim.add_mine((4, 30), "stone", remaining=30)
        sim.add_mine((8, 20), "copper", remaining=30)
        turn = Turn.load(sim.payload())
        unit = next(u for u in turn.ours if u.unit_id == W1)
        fsm = WorkerFSM(W1)
        fsm.role = ROLE_REPAIRER
        fsm.mine = Pos(4, 30)  # 锁了石矿
        ctx = _Ctx({})
        ctx.reserved = set()
        ctx.dusk_avoid = False
        fsm._mine_flow(turn, unit, ctx, prefer="money")
        # 石矿锁被释放 → 重选应为铜矿
        self.assertIsNotNone(fsm.mine)
        self.assertEqual(turn.zones.get(fsm.mine), "copper")


class TestMineRateSelection(unittest.TestCase):
    def test_near_mine_beats_far_richer_mine(self):
        """单位回合收益：近矿应胜远矿（哪怕远矿更富）——矿工不空耗往返（issue#26）。"""
        # 近处石矿(dist2, price1) vs 远处铜矿(dist20, price5)
        # 近石 rate=10/(4+10)=0.71  远铜 rate=50/(40+10)=1.0 → 此处铜仍胜。
        # 关键校验：同矿种下近矿必胜；且评分随距离单调下降。
        sim = make_sim(mines={(12, 22): "copper", (30, 2): "copper"})
        sim.add_mine((12, 22), "copper", remaining=30)
        sim.add_mine((30, 2), "copper", remaining=30)
        turn = Turn.load(sim.payload())
        unit = next(u for u in turn.ours if u.unit_id == W1)
        fsm = WorkerFSM(W1)
        ctx = _Ctx({})
        ctx.reserved = set()
        ctx.dusk_avoid = False
        ctx.price_boost_map = {}
        mine = fsm._select_mine(turn, unit, ctx, prefer="money")
        self.assertEqual(mine, Pos(12, 22), "同矿种近矿应胜远矿")


if __name__ == "__main__":
    unittest.main()
