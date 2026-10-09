"""L4 JointFirePlanner：3 火箭轮转 + AOE 落点评分（STRATEGY_DECISIONS #3）。

- 开拓者单人操控：attack 挂在武器 ID 下，controllerId=开拓者。
- 冷却账本（P0-1，实战 F1/F2）：平台 cooldown 字段恒 0 不可信；
  火箭实际冷却 4 回合——同炮开火间隔 ≤4 全失败（0/145），≥5 全成功（734/734）。
  以「上次成功开火回合 + 5」判定 ready，并用 lastRoundRoleActionResults 结账。
- 连败换炮（P0-1）：同一炮连续 2 次反馈失败 → 本回合跳过让别的炮顶上，trace 留痕。
- 落点评分：中心 20 + 溅射 10/个；回避己方单位 3×3（U10 友伤未明，安全默认）。
- station 受击响应（P1-5）：基地血量较上回合下降时，冲我方的机器人额外加权。
- 无价值目标宁可空仓，不浪费冷却。
"""
from __future__ import annotations

from typing import Any

from .protocol import ROCKET, WALL_MAX_HP, Pos, Turn, Unit, attack_command, distance

CENTER_DAMAGE = 20
SPLASH_DAMAGE = 10
LETHAL_BONUS = 15      # 能杀死的优先（最快降低敌方 DPS）
APPROACH_RADIUS = 12   # 距基地锚点 12 格内开始加权
SELF_TEAM_BONUS = 5    # 以我方基地为目标的机器人优先（两批机器人分攻双方）
STATION_HIT_BONUS = 15  # P1-5：station 掉血时对冲我方机器人的追加权重
# U10 已由实战证据关闭：紧邻己方建筑的机器人必须能打（墙聚怪+火箭隔山打牛是核心玩法），
# 不再扣友伤惩罚（2026-09-21 TeamB 实战：惩罚导致基地被啃时全面哑火）。
MIN_SCORE = CENTER_DAMAGE
ROCKET_READY_GAP = 5   # P0-1 实测：间隔 5 回合起才可能成功（间隔 ≤4 实测 0 成功）
FAIL_STREAK_SKIP = 2   # 连续失败该次数后本回合跳过该炮（换炮）
# ---- G 积分射击（IKKHUU-Q4 用户裁决 2026-10-09）----
ROBOT_POINTS = {
    "smallRobot": 1, "middleRobot": 2, "largeRobot": 4, "bossRobot": 10,
}
POINTS_SCALE = 10      # 积分权重放大系数（压过 approach 等小项）
FIXER_SAFE_STOCK = 8   # 工人 WallFixer 持有 ≥ 此值 → 防线"顶得住"（实测夜耗 1→14 逐日增长）
WALL_CRITICAL_RATIO = 0.35  # 墙血 <35% 满血 → 防线不安全
BIG_TYPES = ("largeRobot", "bossRobot")
BURST_DAMAGE = CENTER_DAMAGE * 3  # 三炮一轮集火伤害（火力评估：能否击杀大型）
BIG_STUCK_PENALTY = 150  # 杀不动的大型目标惩罚（>小型可击杀分，防止浪费冷却）


class JointFirePlanner:
    def __init__(self) -> None:
        self.cursor = 0
        # P0-1 冷却账本
        self._pending: dict[int, int] = {}        # weapon_id -> 本回合已开火，待下回合结账
        self._last_success: dict[int, int] = {}   # weapon_id -> 上次成功开火回合号
        self._fail_streak: dict[int, int] = {}    # weapon_id -> 连续反馈失败次数
        # P1-5 跨回合跟踪
        self._station_hp: int | None = None

    # ---- P0-1：用上回合开火反馈结账 ----
    def _settle(self, turn: Turn, trace: dict[str, Any]) -> None:
        """lastRoundRoleActionResults 只给 False 的角色 ID（无原因码）：
        pending 炮未点名为 False 即视为成功；点了 False 记连败。"""
        if not self._pending:
            return
        results = turn.last_action_results or {}
        for wid, fired_round in list(self._pending.items()):
            if results.get(wid) is False:
                self._fail_streak[wid] = self._fail_streak.get(wid, 0) + 1
                trace.setdefault("fire_fb", []).append(
                    {"weapon": wid, "fired": fired_round, "streak": self._fail_streak[wid]}
                )
            else:
                self._last_success[wid] = fired_round
                self._fail_streak.pop(wid, None)
            self._pending.pop(wid, None)

    def plan(
        self,
        turn: Turn,
        pioneer: Unit,
        commands: dict[int, dict[str, Any]],
        trace: dict[str, Any],
    ) -> None:
        self._settle(turn, trace)

        # P1-5：station 掉血检测（跨回合比较）
        station = turn.station()
        station_hit = False
        if station is not None:
            if self._station_hp is not None and station.health < self._station_hp:
                station_hit = True
            self._station_hp = station.health

        ready: list[Unit] = []
        skipped: list[int] = []
        for w in turn.weapons():
            if distance(pioneer.pos, w.pos) > 1:
                continue
            if w.kind == ROCKET:
                # 账本优先：平台 cooldown 恒 0（F2），仅作兜底
                if w.cooldown != 0:
                    continue
                last_ok = self._last_success.get(w.unit_id)
                if last_ok is not None and turn.round_no - last_ok < ROCKET_READY_GAP:
                    continue
            elif w.cooldown != 0:
                continue
            if self._fail_streak.get(w.unit_id, 0) >= FAIL_STREAK_SKIP:
                skipped.append(w.unit_id)
                continue
            ready.append(w)
        if not ready and skipped:
            # 全部连败则回退使用（宁可试也不空仓），连败计数保留
            ready = [w for w in turn.weapons() if w.unit_id in skipped]
        if not ready:
            info: dict[str, Any] = {"reason": "no_ready_weapon"}
            if skipped:
                info["skipped_fail_streak"] = skipped
            trace["fire"] = info
            return
        ready.sort(key=lambda w: w.unit_id)
        weapon = ready[self.cursor % len(ready)]
        targets, score = self._best_targets(turn, weapon, station_hit)
        if not targets:
            info = {"reason": "no_valuable_target", "weapon": weapon.unit_id}
            if station_hit:
                info["station_hit"] = True
            trace["fire"] = info
            return
        self.cursor += 1
        self._pending[weapon.unit_id] = turn.round_no
        commands[weapon.unit_id] = attack_command(pioneer.unit_id, targets)
        info = {
            "weapon": weapon.unit_id,
            "targets": [t.dump() for t in targets],
            "aoe_score": score,
        }
        if station_hit:
            info["station_hit"] = True
            info["station_hp"] = station.health if station is not None else 0
        trace["fire"] = info

    def _defense_safe(self, turn: Turn) -> bool:
        """G 火力评估前半：防线是否"顶得住"（用户裁决 IKKHUU-Q4）——
        工人 WallFixer 持有充足 + 无濒危墙。station 正在掉血时由调用方排除。"""
        fixers = sum(
            u.backpack.count("WallFixer")
            for u in turn.ours if u.kind == "worker"
        )
        if fixers < FIXER_SAFE_STOCK:
            return False
        for w in turn.walls():
            if w.level >= 3:
                continue
            max_hp = WALL_MAX_HP[min(max(w.level, 1), len(WALL_MAX_HP)) - 1]
            if w.health < WALL_CRITICAL_RATIO * max_hp:
                return False
        return True

    def _best_targets(
        self, turn: Turn, weapon: Unit, station_hit: bool = False
    ) -> tuple[list[Pos], int]:
        reach = weapon.range_of_attack()
        in_range = [
            r
            for r in turn.robots
            if r.alive and 1 <= distance(weapon.pos, r.pos) <= reach
        ]
        if not in_range:
            return (), 0
        # 硬优先：射程内有冲我方来的机器人时，绝不打敌方机器人（实战教训：炮台误击对方机器人）
        ours = [r for r in in_range if r.target_team in ("", turn.team_type)]
        robots = ours if ours else in_range
        station = turn.station()
        anchor = station.pos if station is not None else weapon.pos
        # G 积分模式（IKKHUU-Q4）：防线顶得住（修复包足+无濒危墙）且基地未掉血 →
        # 积分优先；否则维持威胁优先（防御第一）。
        points_mode = self._defense_safe(turn) and not station_hit
        # 大型杀不动判定：血量 > 三炮一轮集火伤害 → 本轮击杀无望
        big_stuck = {
            r.robot_id for r in robots
            if r.kind in BIG_TYPES and r.health > BURST_DAMAGE
        }
        scored: list[tuple[int, int, int, int, int, Pos]] = []
        for robot in robots:
            cell = robot.pos
            # 伤害按 HP 封顶：中心 20、溅射 10
            damage = min(CENTER_DAMAGE, robot.health)
            kills = 1 if damage >= robot.health else 0
            for other in robots:
                if other.robot_id == robot.robot_id or distance(cell, other.pos) > 1:
                    continue
                splash = min(SPLASH_DAMAGE, other.health)
                damage += splash
                if splash >= other.health:
                    kills += 1
            # 致命优先 + 逼近防御圈优先 + 优先攻击以我方为目标的机器人
            # P1-5：station 正在掉血时，冲我方的机器人再加一档权重
            team_bonus = 0
            if robot.target_team == turn.team_type:
                team_bonus = SELF_TEAM_BONUS + (STATION_HIT_BONUS if station_hit else 0)
            if points_mode:
                # 积分主导：可击杀目标 ×2（命中即得分）；杀不动的大型目标重罚——
                # 火力打不死 BOSS 但修复包顶得住 → 转射可击杀的小兵拿分（用户补规则）
                pts = ROBOT_POINTS.get(robot.kind, 1)
                stuck = robot.robot_id in big_stuck
                score = (
                    pts * POINTS_SCALE * (2 if kills else 1)
                    + damage
                    + team_bonus
                    - (BIG_STUCK_PENALTY if stuck else 0)
                )
            else:
                score = (
                    damage
                    + LETHAL_BONUS * kills
                    + max(0, APPROACH_RADIUS - distance(cell, anchor))
                    + team_bonus
                )
            scored.append((score, -robot.health, -distance(weapon.pos, cell), cell.x, cell.y, cell))
        scored.sort(reverse=True)
        n_targets = max(1, weapon.level)
        picked = [
            (item[0], item[5]) for item in scored[:n_targets] if item[0] >= MIN_SCORE
        ]
        if not picked:
            return (), 0
        cells = [cell for _, cell in picked]
        # 接口 §2.2：火箭 targetPos 数量必须与武器等级相同。残局敌人不足时
        # 落点数 < 等级 → 指令非法（夜晚只剩 1-2 敌人时攻击失败的可能真因）。
        # 用已选落点的 8 邻域空地补齐（不出图），仍不足则重复首个落点。
        if len(cells) < n_targets:
            pool: list[Pos] = []
            for cell in cells:
                pool.extend(
                    p for p in cell.neighbours()
                    if p not in pool and p not in cells
                    and 0 <= p.x < turn.width and 0 <= p.y < turn.height
                )
            for p in pool:
                if len(cells) >= n_targets:
                    break
                cells.append(p)
            while len(cells) < n_targets:
                cells.append(cells[0])
        return cells, picked[0][0]
