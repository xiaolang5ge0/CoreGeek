"""L4 JointFirePlanner：多武器轮转 + AOE 落点评分（STRATEGY_DECISIONS #3 → §21）。

- 支持双操控（IKKIBC-Q7 用户裁决：2 火箭+电磁炮，**挖矿工**夜控电磁炮）：
  每回合可由两个角色各操控一座武器；同回合已开火的炮不再派。
- 冷却账本（P0-1，实战 F1/F2）：平台 cooldown 字段恒 0 不可信；
  火箭实际冷却 4 回合——同炮开火间隔 ≤4 全失败（0/145），≥5 全成功（734/734）。
  以「上次成功开火回合 + 5」判定 ready，并用 lastRoundRoleActionResults 结账。
  电磁炮冷却恒 0（接口文档 §1.4）→ 每回合可发。
- 落点分配（IKKIA9-Q2 用户裁决：补刀线贪心）：按优先级逐目标分配弹头，
  小兵 40 血 = 2 发带走 → 集中补刀；杀不动的大目标摊 1 发磨血——
  消除"三门炮打完全相同 3 落点"的超杀浪费。
- 积分模式（IKKHUU-Q4-G）+ **回退集火**（IKKIA9-Q1：积分模式把杀不动的大目标
  全部滤掉 → D4 夜空转 11 回合实锤；过滤后无目标必须回退威胁模式集火磨血——
  它们在啃墙，空转=白送墙血）。
- 后方 BOSS 优先（IKKIBC-Q5 用户裁决：危险度=距离+数量）：大型机器人出现在
  我方"开口侧"（背后，敌方可召唤 BOSS 的方向）→ 按距基地距离与后方大怪数加权。
- 电磁炮弹道（§4.5.1）：能量 = 20×等级，沿直线穿透，路径上每只机器人受
  min(剩余能量, 血量) 伤害；选"弹道总收益"最大的目标。单目标（接口 §2.2）。
- 落点评分：中心 20 + 溅射 10/个；回避己方单位 3×3 已由实战证据关闭（U10）。
- station 受击响应（P1-5）：基地掉血时冲我方的机器人额外加权。
"""
from __future__ import annotations

from typing import Any

from .protocol import (
    RAILGUN,
    ROCKET,
    WALL_MAX_HP,
    Pos,
    Turn,
    Unit,
    attack_command,
    distance,
)

CENTER_DAMAGE = 20
SPLASH_DAMAGE = 10
LETHAL_BONUS = 15      # 能杀死的优先（最快降低敌方 DPS）
APPROACH_RADIUS = 12   # 距基地锚点 12 格内开始加权
SELF_TEAM_BONUS = 5    # 以我方基地为目标的机器人优先（两批机器人分攻双方）
STATION_HIT_BONUS = 15  # P1-5：station 掉血时对冲我方机器人的追加权重
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
BIG_STUCK_PENALTY = 150  # 杀不动的大型目标惩罚（>小型可击杀分，防止浪费冷却）
# ---- Q5 后方 BOSS（IKKIBC 用户裁决：危险度 = 距离 + 数量）----
REAR_BIG_BONUS = 30    # 后方（开口侧）大型机器人的基础加权（压过 approach 小项）
REAR_BIG_COUNT_BONUS = 12  # 每多一只后方大怪，全体后方大怪再 +12（成群=高危）
RAILGUN_DMG = 20       # 电磁炮能量基数（×等级）


class JointFirePlanner:
    def __init__(self) -> None:
        self.cursor = 0
        # P0-1 冷却账本
        self._pending: dict[int, int] = {}        # weapon_id -> 本回合已开火，待下回合结账
        self._last_success: dict[int, int] = {}   # weapon_id -> 上次成功开火回合号
        self._fail_streak: dict[int, int] = {}    # weapon_id -> 连续反馈失败次数
        self._fired_round: dict[int, int] = {}    # weapon_id -> 本（结算）回合已开火（双操控防同回合重复派）
        # P1-5 跨回合跟踪
        self._station_hp: int | None = None
        # Q5 后方轴向（brain 在布局确定后注入；"" = 未知 → 后方加成关闭）
        self.front = ""
        self._station_min: Pos | None = None

    # ---- P0-1：用上回合开火反馈结账 ----
    def _settle(self, turn: Turn, trace: dict[str, Any]) -> None:
        """lastRoundRoleActionResults 只给 False 的角色 ID（无原因码）：
        pending 炮未点名为 False 即视为成功；点了 False 记连败。
        双操控（Q7）：本回合刚打的炮（fired_round == 当前回合）不参与结账。"""
        if not self._pending:
            return
        results = turn.last_action_results or {}
        for wid, fired_round in list(self._pending.items()):
            if fired_round >= turn.round_no:
                continue  # 本回合刚开火 → 留给下回合结账
            if results.get(wid) is False:
                self._fail_streak[wid] = self._fail_streak.get(wid, 0) + 1
                trace.setdefault("fire_fb", []).append(
                    {"weapon": wid, "fired": fired_round, "streak": self._fail_streak[wid]}
                )
            else:
                self._last_success[wid] = fired_round
                self._fail_streak.pop(wid, None)
            self._pending.pop(wid, None)

    def set_front(self, front: str, station_pos: Pos | None) -> None:
        """brain 布局确定后注入开口朝向与基地锚点（Q5 后方判定用）。"""
        self.front = front or ""
        self._station_min = station_pos

    def _is_rear(self, turn: Turn, cell: Pos) -> bool:
        """后方 = 基地"开口侧"（FRONT 朝向一侧）——敌方召唤 BOSS 的来向（IKKIBC 实锤）。"""
        st = self._station_min
        if st is None or not self.front:
            return False
        if self.front == "W":
            return cell.x < st.x
        if self.front == "E":
            return cell.x > st.x + 1
        if self.front == "N":
            return cell.y < st.y
        return cell.y > st.y + 1  # S

    def plan(
        self,
        turn: Turn,
        controller: Unit,
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
            if distance(controller.pos, w.pos) > 1:
                continue
            if w.unit_id in self._fired_round and self._fired_round[w.unit_id] >= turn.round_no:
                continue  # 本回合已被另一操控者打过
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
            ready = [
                w for w in turn.weapons()
                if w.unit_id in skipped
                and distance(controller.pos, w.pos) <= 1
                and not (w.unit_id in self._fired_round
                         and self._fired_round[w.unit_id] >= turn.round_no)
            ]
        if not ready:
            info: dict[str, Any] = {"reason": "no_ready_weapon"}
            if skipped:
                info["skipped_fail_streak"] = skipped
            trace["fire"] = info
            return
        # 统一轮转 + **轮转回退**（Q7 复盘：电磁炮无冷却 → 火箭冷却间隙里它天然
        # 每回合都在 ready 列表，轮到即发；选中的炮无目标时试下一门，不浪费回合）。
        ready.sort(key=lambda w: w.unit_id)
        start = self.cursor % len(ready)
        self.cursor += 1
        weapon = None
        targets: tuple = ()
        score = 0
        for off in range(len(ready)):
            cand = ready[(start + off) % len(ready)]
            t, sc = self._best_targets(turn, cand, station_hit)
            if t:
                weapon, targets, score = cand, t, sc
                break
        if weapon is None:
            info = {"reason": "no_valuable_target", "weapon": ready[start].unit_id}
            if station_hit:
                info["station_hit"] = True
            trace["fire"] = info
            return
        self._fired_round[weapon.unit_id] = turn.round_no
        self._pending[weapon.unit_id] = turn.round_no
        commands[weapon.unit_id] = attack_command(controller.unit_id, targets)
        info: dict[str, Any] = {
            "weapon": weapon.unit_id,
            "kind": weapon.kind,
            "controller": controller.unit_id,
            "targets": [t.dump() for t in targets],
            "aoe_score": score,
        }
        if station_hit:
            info["station_hit"] = True
            info["station_hp"] = station.health if station is not None else 0
        # 双操控：同回合两座武器都开火时 trace 两条都留（fire=首选，fire_b=次选）
        if isinstance(trace.get("fire"), dict) and trace["fire"].get("round") == turn.round_no:
            trace["fire_b"] = info
        else:
            info["round"] = turn.round_no
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

    # ---- 评分（威胁模式 / 积分模式共用）----
    def _mode_score(
        self, turn: Turn, robot, damage: int, kills: int,
        station_hit: bool, points_mode: bool, rear_bigs: int,
    ) -> int:
        cell = robot.pos
        team_bonus = 0
        if robot.target_team == turn.team_type:
            team_bonus = SELF_TEAM_BONUS + (STATION_HIT_BONUS if station_hit else 0)
        rear_bonus = 0
        # Q5（IKKIBC 用户裁决）：后方（开口侧）大型机器人 = 敌方召唤 BOSS 嫌疑，
        # 危险度 = 距离 + 数量：距基地越近、数量越多 → 越优先打。
        if robot.kind in BIG_TYPES and self._is_rear(turn, cell):
            station = turn.station()
            anchor = station.pos if station is not None else cell
            rear_bonus = (
                REAR_BIG_BONUS
                + max(0, APPROACH_RADIUS - distance(cell, anchor)) * 2
                + REAR_BIG_COUNT_BONUS * max(0, rear_bigs - 1)
            )
        if points_mode:
            pts = ROBOT_POINTS.get(robot.kind, 1)
            return (
                pts * POINTS_SCALE * (2 if kills else 1)
                + damage
                + team_bonus
                + rear_bonus
            )
        return (
            damage
            + LETHAL_BONUS * kills
            + max(0, APPROACH_RADIUS - distance(cell, self._anchor_or(turn, cell)))
            + team_bonus
            + rear_bonus
        )

    def _anchor_or(self, turn: Turn, fallback: Pos) -> Pos:
        station = turn.station()
        return station.pos if station is not None else fallback

    def _best_targets(
        self, turn: Turn, weapon: Unit, station_hit: bool = False
    ) -> tuple[list[Pos], int]:
        # IKKIBC-Q6：绝不打自家召唤机器人
        in_range = [
            r
            for r in turn.hostile_robots
            if 1 <= distance(weapon.pos, r.pos) <= weapon.range_of_attack()
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
        burst = CENTER_DAMAGE * 3
        big_stuck = {
            r.robot_id for r in robots
            if r.kind in BIG_TYPES and r.health > burst
        }
        # Q5：后方大怪数量（危险度要素）
        rear_bigs = sum(
            1 for r in robots
            if r.kind in BIG_TYPES and self._is_rear(turn, r.pos)
        )
        scored: list[tuple[int, int, int, int, int, Any]] = []
        scored_thr: list[tuple[int, int, int, int, int, Any]] = []
        for robot in robots:
            cell = robot.pos
            damage = min(CENTER_DAMAGE, robot.health)
            kills = 1 if damage >= robot.health else 0
            for other in robots:
                if other.robot_id == robot.robot_id or distance(cell, other.pos) > 1:
                    continue
                splash = min(SPLASH_DAMAGE, other.health)
                damage += splash
                if splash >= other.health:
                    kills += 1
            stuck = robot.robot_id in big_stuck
            thr = self._mode_score(
                turn, robot, damage, kills, station_hit, False, rear_bigs
            )
            scored_thr.append((thr, -robot.health, -distance(weapon.pos, cell), cell.x, cell.y, robot.robot_id, robot))
            if points_mode:
                pts = self._mode_score(
                    turn, robot, damage, kills, station_hit, True, rear_bigs
                )
                if stuck:
                    pts -= BIG_STUCK_PENALTY
                scored.append((pts, -robot.health, -distance(weapon.pos, cell), cell.x, cell.y, robot.robot_id, robot))
        scored.sort(reverse=True)
        scored_thr.sort(reverse=True)

        # ---- 电磁炮：单目标，弹道收益评估（§4.5.1 能量穿透）----
        if weapon.kind == RAILGUN:
            best_cell, best_val = None, 0
            for item in scored_thr:
                robot = item[6]
                beam_dmg, beam_kills = self._railgun_beam(turn, weapon, robot.pos, robots)
                val = self._mode_score(
                    turn, robot, beam_dmg, beam_kills, station_hit, False, rear_bigs
                )
                if val > best_val:
                    best_cell, best_val = robot.pos, val
            if best_cell is None or best_val < MIN_SCORE:
                return (), 0
            return [best_cell], best_val

        # ---- Q1 回退（IKKIA9 实锤）：积分模式把杀不动的大目标全部滤掉 →
        # 无目标可打 ≠ 不打：**用威胁分重选**集火磨血（它们在啃墙，空转=白送墙血）。----
        n_targets = max(1, weapon.level)
        picked_pts = [item for item in scored[:n_targets] if item[0] >= MIN_SCORE]
        if picked_pts:
            order = [item[6] for item in picked_pts]
            mode = "points"
        else:
            threat_picked = [item for item in scored_thr if item[0] >= MIN_SCORE]
            if not threat_picked:
                return (), 0
            order = [item[6] for item in threat_picked[:n_targets]]
            mode = "grind"
        # ---- Q2 补刀线贪心（IKKIA9 用户裁决）：按优先级逐目标分配弹头——
        # ceil(hp/20) 发带走一个目标，杀不动/剩余弹头摊开磨血。----
        cells = self._allocate_warheads(order, n_targets)
        # 接口 §2.2：火箭 targetPos 数量必须与武器等级相同；不足用 8 邻域空地补齐。
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
        score0 = (scored[0][0] if scored else (scored_thr[0][0] if scored_thr else 0))
        if mode == "grind":
            score0 = -1  # 仅作 trace 标记（回退集火）
        return cells, score0

    def _allocate_warheads(self, order: list, n_targets: int) -> list[Pos]:
        """补刀线贪心：优先级序逐目标分配弹头。

        - hp ≤ 20×剩余弹头 → 集中 ceil(hp/20) 发带走（小兵 40 血 = 2 发）；
        - 杀不动（需弹头 > 剩余）→ 只摊 1 发磨血，弹头留给后面的可杀目标；
        - 全部分配完仍有剩余 → 回头给最高优先目标补发（磨血）。
        同一目标多弹头 = targetPos 重复该坐标（接口未要求去重；残局补齐已有先例）。
        """
        cells: list[Pos] = []
        left = n_targets
        chip: list[Any] = []
        for robot in order:
            if left <= 0:
                break
            need = max(1, -(-robot.health // CENTER_DAMAGE))  # ceil
            if need <= left:
                cells.extend([robot.pos] * need)
                left -= need
            else:
                cells.append(robot.pos)
                chip.append(robot)
                left -= 1
        while left > 0 and order:
            cells.append(order[0].pos)   # 剩余弹头磨最高优先目标
            left -= 1
        return cells

    def _railgun_beam(
        self, turn: Turn, weapon: Unit, target: Pos, robots: list
    ) -> tuple[int, int]:
        """电磁炮弹道收益（§4.5.1）：能量 20×等级沿直线穿透，
        路径上每只存活机器人受 min(剩余能量, 血量)，能量按伤害扣减。"""
        energy = RAILGUN_DMG * max(1, weapon.level)
        total = 0
        kills = 0
        by_cell: dict[Pos, list] = {}
        for r in robots:
            by_cell.setdefault(r.pos, []).append(r)
        for cell in _line_cells(weapon.pos, target):
            if cell == weapon.pos:
                continue
            for r in by_cell.get(cell, ()):
                dmg = min(energy, r.health)
                if dmg <= 0:
                    continue
                total += dmg
                if dmg >= r.health:
                    kills += 1
                energy -= dmg
                if energy <= 0:
                    return total, kills
        return total, kills


def _line_cells(a: Pos, b: Pos) -> list[Pos]:
    """两点中心连线穿过的格子序列（从 a 到 b，含端点；采样步长 0.1 格）。"""
    out: list[Pos] = []
    ax, ay = a.x + 0.5, a.y + 0.5
    bx, by = b.x + 0.5, b.y + 0.5
    steps = max(1, int(max(abs(bx - ax), abs(by - ay)) * 10))
    seen: set[tuple[int, int]] = set()
    for i in range(steps + 1):
        t = i / steps
        x = ax + (bx - ax) * t
        y = ay + (by - ay) * t
        key = (int(x), int(y))
        if key in seen:
            continue
        seen.add(key)
        out.append(Pos(key[0], key[1]))
    return out
