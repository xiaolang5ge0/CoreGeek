"""L2 WallRegistry：按位置维护围墙状态字典（是否存在 / 等级 / 血量 / 是否正面）。

用户补充说明（2026-09-22）落地：
- 围墙/武器升到 L3 后升级券失效 → 满级墙只能用 WallFixer 修复（阈值：血量 <50%）。
- 前夜被攻破的墙，次日白天必须补建；补建后回到 L1，必须重新纳入升级队列。
- 用 Dict 维护每个位置的墙况（exists/health/level），不依赖单回合快照。

边界：只记录"事实"（存在/等级/血量/缺口/补建事件），不产出行动建议。
"""
from __future__ import annotations

from dataclasses import dataclass

from .protocol import Pos, Turn, WALL_MAX_HP, distance

REPAIR_HP_RATIO = 0.5  # 围墙修复阈值：血量 <50% 才值得动用修复包/升级券（用户补充）
FRONT_RATIO = 0.5      # 距 CP 最远的前一半墙视为正面（与升级器 FRONT 口径一致）


@dataclass
class WallState:
    pos: Pos
    exists: bool = False
    level: int = 0
    health: int = 0
    is_front: bool = False
    breached_round: int = 0  # 最近一次由存在→不存在（被攻破）的回合
    rebuilt_round: int = 0   # 最近一次由不存在→存在（补建）的回合

    @property
    def max_health(self) -> int:
        return WALL_MAX_HP[min(max(self.level, 1), 3) - 1]

    @property
    def ratio(self) -> float:
        if not self.exists:
            return 0.0
        return self.health / self.max_health

    @property
    def damaged(self) -> bool:
        """受损到需要修复/升级回血（<50%）。"""
        return self.exists and self.ratio < REPAIR_HP_RATIO

    @property
    def maxed(self) -> bool:
        """已到最高等级（L3）→ 升级券失效，只能用 WallFixer。"""
        return self.level >= 3

    @property
    def upgradable(self) -> bool:
        return self.exists and self.level < 3

    def dump(self) -> dict:
        return {
            "pos": self.pos.dump(),
            "exists": self.exists,
            "level": self.level,
            "health": self.health,
            "is_front": self.is_front,
            "breached_round": self.breached_round,
            "rebuilt_round": self.rebuilt_round,
        }


class WallRegistry:
    """跨回合维护的围墙状态表（key = Pos）。"""

    def __init__(self) -> None:
        self.walls: dict[Pos, WallState] = {}
        self.cp: Pos | None = None

    def sync(self, turn: Turn, layout=None) -> None:
        """用本回合快照 + 布局刷新墙况，识别"被攻破"与"补建"事件。

        - 布局指定格即使尚未建造也登记（便于识别缺口/补建）。
        - 由存在→不存在记 breached_round；由不存在→存在记 rebuilt_round。
        """
        present = {w.pos: w for w in turn.walls()}
        front: set = set()
        if layout is not None:
            self.cp = layout.control_point
            for cell in layout.wall_cells:
                self.walls.setdefault(cell, WallState(cell))
            ordered = sorted(
                layout.wall_cells,
                key=lambda p: (-distance(p, layout.control_point), p.x, p.y),
            )
            front = set(ordered[: max(1, round(len(ordered) * FRONT_RATIO))])

        for pos, state in self.walls.items():
            wall = present.get(pos)
            if wall is not None:
                if not state.exists:
                    state.rebuilt_round = turn.round_no
                state.exists = True
                state.level = wall.level
                state.health = wall.health
            else:
                if state.exists:
                    state.breached_round = turn.round_no
                state.exists = False
                state.health = 0
            state.is_front = pos in front

        # 登记非布局位置的墙（布局迁移 / 历史遗留），避免状态表遗漏
        for pos, wall in present.items():
            if pos in self.walls:
                continue
            self.walls[pos] = WallState(
                pos, exists=True, level=wall.level, health=wall.health
            )

    # ---- 查询 ----
    def existing(self) -> list[WallState]:
        return [s for s in self.walls.values() if s.exists]

    def missing(self) -> list[WallState]:
        """布局内当前缺失的墙（含尚未建造与已攻破）。"""
        return [s for s in self.walls.values() if not s.exists]

    def breached(self) -> list[WallState]:
        """曾被攻破的墙（历史事件），按最近一次攻破回合排序。"""
        return sorted(
            (s for s in self.walls.values() if s.breached_round > 0),
            key=lambda s: s.breached_round,
        )

    def recently_rebuilt(self) -> list[WallState]:
        """补建过且当前仍在的墙（回到 L1 → 需重新升级）。"""
        return [s for s in self.walls.values() if s.exists and s.rebuilt_round > 0]

    def damaged(self) -> list[WallState]:
        """受损(<50%)墙，正面优先、血量比例升序。"""
        return sorted(
            (s for s in self.walls.values() if s.damaged),
            key=lambda s: (0 if s.is_front else 1, s.ratio, s.pos.x, s.pos.y),
        )

    def upgradable(self) -> list[WallState]:
        """未满级且存在的墙，受损 > 补建 > 正面 > 其余。"""
        def key(s: WallState):
            band = 0 if s.damaged else (1 if s.rebuilt_round > 0 else (2 if s.is_front else 3))
            return (band, s.ratio, s.pos.x, s.pos.y)

        return sorted(
            (s for s in self.walls.values() if s.upgradable), key=key
        )

    def maxed_damaged(self) -> list[WallState]:
        """满级(L3)且受损的墙 → 升级券无效，只能用 WallFixer。"""
        return [s for s in self.walls.values() if s.maxed and s.damaged]

    def dump(self) -> dict:
        return {
            "walls": [s.dump() for s in sorted(self.walls.values(), key=lambda s: (s.pos.x, s.pos.y))],
            "missing": len(self.missing()),
            "damaged": len(self.damaged()),
        }
