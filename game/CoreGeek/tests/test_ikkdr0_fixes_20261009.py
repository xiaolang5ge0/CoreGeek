"""IKKDR0 四项裁决回归（2026-10-09）：
Q1 imp 选矿按"离敌方基地更近"判定（替代对角线）+ 距离进评分；
Q2 工人夜间动态避让：去程被打断 ≥3 次弃矿 + 选矿路径机器人过滤 + 粘性到期路径有机器人续等；
Q3 正面 L3 死线未达标 → 墙券按缺口一次买齐（优先级 -1，解除 6 张上限与 D4 帽）；
Q4 敌 imp 破坏我方矿 → 矿种禁采 10 回合（brain 学习 + 工人选矿跳过）。
"""
import unittest

import _bootstrap  # noqa: F401

from agent.brain import Brain
from agent.fsm_imp import ImpFSM
from agent.fsm_worker import WorkerFSM
from agent.planners.upgrade import UpgradePlanner
from agent.protocol import Pos, Turn
from test_v2_rules import base_payload


def _imp_ctx():
    class Ctx:
        trace = {}
        reserved = frozenset()
        robot_cells = ()
        note = staticmethod(lambda *a, **k: None)
    return Ctx()


class _WorkerCtx:
    blocked = []

    def __init__(self, imp_ban=None):
        self.trace = {}
        self.reserved = frozenset()
        self.robot_cells = ()
        self.share_mines = False
        self.dusk_avoid = False
        self.imp_mine_ban = imp_ban or {}
        self.mine_blacklist = {}
        _WorkerCtx.blocked = []

    def is_mine_blocked(self, pos, round_no):
        return False

    def other_mine_locks(self, uid):
        return ()

    def mine_unsafe(self, pos):
        return False

    def note(self, *a, **k):
        pass

    def block_mine(self, pos, release):
        _WorkerCtx.blocked.append((pos, release))

    def price_boost(self, kind):
        return 0.0


def _robot(x, y, target=""):
    return {"id": 90001, "pos": {"x": x, "y": y}, "roleType": "smallRobot",
            "health": 40, "abnormalState": "", "targetTeam": target}


def _worker10010(turn):
    return next(u for u in turn.ours if u.kind == "worker" and u.unit_id == 10010)


class TestQ1ImpTargetByEnemyBase(unittest.TestCase):
    """Q1：imp 目标矿 = 离敌方基地更近的矿（对角线作废）；距离进评分。"""

    def test_diagonal_enemy_but_near_our_base_is_skipped(self):
        # (7,3)：对角线敌方三角，但离我方基地 (10,24) 更近（20 vs 27）→ 不拆
        # (25,8)：对角线敌方、离敌方基地 (30,10) 近（5 vs 21）→ 拆它
        payload = base_payload(40)
        payload["mapInfo"]["zones"] = [
            {"neutralType": "copper", "pos": {"x": 7, "y": 3}, "remain": 10},
            {"neutralType": "iron", "pos": {"x": 25, "y": 8}, "remain": 10},
            {"neutralType": "vendor", "pos": {"x": 20, "y": 16}, "remain": None},
            {"neutralType": "weaponShop", "pos": {"x": 25, "y": 20}, "remain": None},
        ]
        turn = Turn.load(payload)
        fsm = ImpFSM()
        fsm.decide(turn, turn.imp(), _imp_ctx())
        self.assertEqual(fsm.destroy_target, Pos(25, 8),
                         "对角线敌方但离我方更近的矿不应再被选中")

    def test_diagonal_ours_but_near_enemy_base_is_targeted(self):
        # (28,22)：对角线我方三角（y=0.775x 线上方 0.3 格），但离敌方基地 (30,10)
        # 更近（12 vs 18）→ 属敌方经济圈，应纳入破坏目标
        payload = base_payload(40)
        payload["mapInfo"]["zones"] = [
            {"neutralType": "copper", "pos": {"x": 28, "y": 22}, "remain": 10},
            {"neutralType": "vendor", "pos": {"x": 20, "y": 16}, "remain": None},
            {"neutralType": "weaponShop", "pos": {"x": 25, "y": 20}, "remain": None},
        ]
        turn = Turn.load(payload)
        fsm = ImpFSM()
        fsm.decide(turn, turn.imp(), _imp_ctx())
        self.assertEqual(fsm.destroy_target, Pos(28, 22),
                         "对角线我方但贴近敌方基地的矿应纳入破坏目标")

    def test_distance_enters_score(self):
        # 同为敌方矿：近的低价铁矿 (25,8)（dist1）应胜过远的铜矿 (28,22)（dist15）
        # 纯价：铜 80 > 铁 50；距离加权：铁 50/1.1=45 > 铜 80/2.5=32
        payload = base_payload(40)
        payload["mapInfo"]["zones"] = [
            {"neutralType": "copper", "pos": {"x": 28, "y": 22}, "remain": 10},
            {"neutralType": "iron", "pos": {"x": 25, "y": 8}, "remain": 10},
            {"neutralType": "vendor", "pos": {"x": 20, "y": 16}, "remain": None},
            {"neutralType": "weaponShop", "pos": {"x": 25, "y": 20}, "remain": None},
        ]
        turn = Turn.load(payload)
        fsm = ImpFSM()
        fsm.decide(turn, turn.imp(), _imp_ctx())
        self.assertEqual(fsm.destroy_target, Pos(25, 8), "距离进评分后近矿优先")


class TestQ2WorkerDynamicAvoid(unittest.TestCase):
    """Q2：去程被打断 ≥3 次弃矿；选矿跳过路径穿机器人流的候选。"""

    def _night(self, round_no, robots):
        payload = base_payload(round_no)
        payload["robot"]["roles"] = robots
        return Turn.load(payload)

    def test_three_evade_trips_abandon_mine(self):
        fsm = WorkerFSM(10010)
        fsm.mine = Pos(27, 30)
        ctx = _WorkerCtx()
        rn = 201
        for _ in range(3):
            turn = self._night(rn, [_robot(12, 21)])  # 机器人贴 worker 10010 (11,22) 距离 1
            fsm._evade_cmd(turn, _worker10010(turn), ctx)
            rn += 6  # 跳出粘性窗口，制造"新一轮打断"
        self.assertIsNone(fsm.mine, "3 次被打断后应弃矿")
        self.assertTrue(any(p == Pos(27, 30) for p, _ in _WorkerCtx.blocked),
                        "弃矿同时应拉黑该矿")

    def test_select_mine_skips_robot_path_candidates(self):
        payload = base_payload(201)  # 夜
        # 富铜矿 (27,30)：路径 (11,22)→(27,30) 中点 (19,26) 附近放机器人 → 路径被穿
        # 近铁矿 (13,21)：路径干净
        payload["mapInfo"]["zones"] = [
            {"neutralType": "copper", "pos": {"x": 27, "y": 30}, "remain": 10},
            {"neutralType": "iron", "pos": {"x": 13, "y": 21}, "remain": 10},
            {"neutralType": "vendor", "pos": {"x": 20, "y": 16}, "remain": None},
            {"neutralType": "weaponShop", "pos": {"x": 25, "y": 20}, "remain": None},
        ]
        payload["robot"]["roles"] = [_robot(19, 26), _robot(20, 25)]
        turn = Turn.load(payload)
        fsm = WorkerFSM(10010)
        got = fsm._select_mine(turn, _worker10010(turn), _WorkerCtx(), prefer="money")
        self.assertEqual(got, Pos(13, 21), "路径穿机器人流的富矿应被跳过，选路径干净的矿")

    def test_sticky_expired_but_path_occupied_keeps_evading(self):
        fsm = WorkerFSM(10010)
        fsm.mine = Pos(27, 30)
        fsm._evade_until = 202  # 粘性本回合到期
        turn = self._night(201, [_robot(15, 24)])  # 机器人不贴身(距离4)、但在去矿路径上
        cmd = fsm._evade_cmd(turn, _worker10010(turn), _WorkerCtx())
        self.assertTrue(201 < fsm._evade_until, "粘性到期但路径前方有机器人 → 应续等")


class TestQ3VoucherBatch(unittest.TestCase):
    """Q3：正面 L3 死线未达标 → V1/V2 按缺口全量、优先级 -1、不受 D4 帽限制。

    布局：station (10,24) → anchor (10,23)，front=W → 正面列 x=13。
    """

    def _payload(self):
        payload = base_payload(521)  # day5
        walls = [
            {"x": 13, "y": 19, "lv": 1}, {"x": 13, "y": 20, "lv": 2},
            {"x": 13, "y": 21, "lv": 1}, {"x": 13, "y": 22, "lv": 3},
            {"x": 13, "y": 23, "lv": 2}, {"x": 13, "y": 24, "lv": 3},
            {"x": 8, "y": 19, "lv": 1}, {"x": 10, "y": 19, "lv": 1},
        ]
        roles = payload["teamOur"]["roles"]
        for i, w in enumerate(walls):
            roles.append({"id": 200000 + i, "pos": {"x": w["x"], "y": w["y"]},
                          "roleType": "wall", "health": 1000, "attackPower": 0,
                          "attackRange": 0, "level": w["lv"], "isDriving": False})
        for j, p in enumerate(((11, 20), (11, 22), (10, 20))):
            roles.append({"id": 300000 + j, "pos": {"x": p[0], "y": p[1]},
                          "roleType": "rocket", "health": 2000, "attackPower": 20,
                          "attackRange": 10, "level": 3, "cooldown": 0, "isDriving": False})
        payload["teamOur"]["goldNum"] = 600
        return payload

    def test_deadline_unmet_full_gap_batch(self):
        turn = Turn.load(self._payload())
        missions = UpgradePlanner().plan(turn, cp=Pos(9, 21), front="W")
        v1 = [m for m in missions if m.voucher == "WallUpgradeVoucher1" and m.kind == "stock"]
        v2 = [m for m in missions if m.voucher == "WallUpgradeVoucher2" and m.kind == "stock"]
        self.assertTrue(v1 and v2, "死线未达标应派墙券备货")
        # IKKE6Q-Q2-A：需求扩到全部墙——正面 L1×2（目标L3 需V1）+ 侧墙 L1×2（目标L2 需V1）= 4；
        # V2 = 正面 L1×2 + L2×2（目标L3）= 4（侧墙目标 L2 不需 V2）
        self.assertEqual(v1[0].qty, 4)
        self.assertEqual(v2[0].qty, 4)
        self.assertEqual(v1[0].priority, -1, "死线未达标应插队 WallFixer 之前")
        self.assertEqual(v2[0].priority, -1)

    def test_deadline_met_normal_batch(self):
        payload = self._payload()
        for r in payload["teamOur"]["roles"]:
            if r.get("roleType") == "wall" and r["pos"]["x"] == 13:
                r["level"] = 3
        turn = Turn.load(payload)
        missions = UpgradePlanner().plan(turn, cp=Pos(9, 21), front="W")
        stocks = [m for m in missions
                  if m.voucher.startswith("WallUpgradeVoucher") and m.kind == "stock"]
        for m in stocks:
            self.assertNotEqual(m.priority, -1, "死线达标后不再插队")
        # IKKE6Q-Q2-A 核心断言：正面全 L3 后，侧墙 L1（目标 L2）仍能拿到券（修空转）
        v1 = [m for m in stocks if m.voucher == "WallUpgradeVoucher1"]
        self.assertTrue(v1, "正面达标后侧墙 L1×2 的 V1 需求必须仍被统计")
        self.assertEqual(v1[0].qty, 2)

    def test_worker_voucher_qty_counts_side_walls(self):
        """修理工实际买券数量 `_voucher_qty` 同样按全墙目标缺口（与派单口径一致）。"""
        from agent.fsm_worker import WorkerFSM

        class _Ctx:
            layout_anchor = (10, 23)
            layout_front = "W"
            weapon_need_l2 = False

        payload = self._payload()
        for r in payload["teamOur"]["roles"]:       # 正面全 L3，只剩侧墙 L1×2
            if r.get("roleType") == "wall" and r["pos"]["x"] == 13:
                r["level"] = 3
        turn = Turn.load(payload)
        unit = next(u for u in turn.ours if u.kind == "worker")
        fsm = WorkerFSM(unit.unit_id)
        # 侧墙 L1×2 目标 L2 → V1 需求 2；V2 需求 0（目标 L2 不需 V2）
        self.assertEqual(
            fsm._voucher_qty(turn, unit, _Ctx(), "WallUpgradeVoucher1", 20, "wall"), 2)
        self.assertEqual(
            fsm._voucher_qty(turn, unit, _Ctx(), "WallUpgradeVoucher2", 30, "wall"), 0)


class TestQ4ImpMineBan(unittest.TestCase):
    """Q4：敌 imp 破坏我方矿 → 矿种禁采 10 回合；工人选矿跳过禁采矿种。"""

    def test_brain_learns_ban_from_destroyed_mine(self):
        brain = Brain()
        # r40：我方半区铜矿 (11,19) + 可见敌 imp 邻接站桩
        p1 = base_payload(40)
        p1["mapInfo"]["zones"].append(
            {"neutralType": "copper", "pos": {"x": 11, "y": 19}, "remain": 5})
        p1["teamEnemy"]["roles"].append(
            {"id": 20014, "pos": {"x": 12, "y": 19}, "roleType": "imp",
             "health": 500, "attackPower": 0, "attackRange": 0})
        brain.decide(p1)
        # r41：铜矿消失（被敌 imp 拆完）→ 对比上一回合快照触发
        p2 = base_payload(41)
        _, t2 = brain.decide(p2)
        self.assertIn("imp_mine_ban", t2, "矿消失+敌imp曾在旁 → 应登记禁采")
        self.assertEqual(brain.imp_mine_ban.get("copper"), 51)

    def test_worker_skips_banned_ore(self):
        payload = base_payload(40)
        payload["mapInfo"]["zones"] = [
            {"neutralType": "copper", "pos": {"x": 27, "y": 30}, "remain": 10},
            {"neutralType": "iron", "pos": {"x": 13, "y": 21}, "remain": 10},
            {"neutralType": "vendor", "pos": {"x": 20, "y": 16}, "remain": None},
            {"neutralType": "weaponShop", "pos": {"x": 25, "y": 20}, "remain": None},
        ]
        turn = Turn.load(payload)
        fsm = WorkerFSM(10010)
        got = fsm._select_mine(turn, _worker10010(turn),
                               _WorkerCtx({"copper": 999}), prefer="money")
        self.assertEqual(got, Pos(13, 21), "铜被禁采时应跳过铜矿选铁矿")


if __name__ == "__main__":
    unittest.main()
