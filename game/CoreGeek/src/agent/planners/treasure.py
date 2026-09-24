"""L3 TreasurePlanner：长上下文类任务（民间传闻 → 祭坛宝藏）。

机制（任务书 §5.2）：民间传闻逐日累积；开拓者据线索推断**宝藏地点/开启条件(祭品)/开启时间**，
携带任务用品到祭坛 `summonTreasure`。全图唯一，成功开启后不重复得奖。

地点/祭品无法确定性推断 → 本实现为**框架 + LLM 推断**：
- 逐日累积 folkLegends（去重）；
- 线索足够且无 ready 计划时，用 LLM 推断（任务期免费；非任务期用每日少量额度）；
- **只有得到 `ready` 计划才行动**：备齐祭品（缺则去商店买）→ 走到祭坛旁 → summonTreasure。

边界：不做任何无把握的献祭（避免浪费金币/用品）；未 ready 时返回 None。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..path import step_toward
from ..protocol import (
    Pos,
    Turn,
    Unit,
    buy_command,
    distance,
    move_command,
    summon_treasure_command,
)

# 任务用品英文名（任务书 §4.6.3）——LLM 返回值须落在此集合内
TREASURE_ITEMS = (
    "AcientTablet", "StarSand", "FlameBreath", "FrostPotion", "ThornAmulet", "IronWhistle",
)
_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


@dataclass
class TreasurePlan:
    location: Pos | None = None
    items: tuple[str, ...] = ()
    day: int = 0
    ready: bool = False
    raw: str = ""

    def dump(self) -> dict:
        return {
            "location": self.location.dump() if self.location else None,
            "items": list(self.items),
            "day": self.day,
            "ready": self.ready,
        }


class TreasurePlanner:
    def __init__(self) -> None:
        self.legends: list[str] = []
        self.plan = TreasurePlan()
        self.attempted = False
        self._last_infer_len = 0   # 上次推断时的线索条数（新线索到来才再问 LLM，用户 2026-09-23）
        self.failed_sites: list[tuple[int, int]] = []   # 召唤失败过的坐标（结果 2/3，需换地点重推）

    # ---- 线索累积 ----
    def observe(self, folk: str) -> bool:
        text = (folk or "").strip()
        if not text or text in self.legends:
            return False
        self.legends.append(text)
        return True

    def mark_inferred(self) -> None:
        """记录"已就当前线索问过 LLM"，避免同一批线索反复提问。"""
        self._last_infer_len = len(self.legends)

    def is_failed_site(self, pos: Pos | None) -> bool:
        return pos is not None and (pos.x, pos.y) in self.failed_sites

    def needs_inference(self) -> bool:
        # 有新线索（条数增加）且计划未 ready → 再问 LLM（用户 2026-09-23：不是一次不成就放弃）
        return (
            bool(self.legends)
            and not self.plan.ready
            and not self.attempted
            and len(self.legends) > self._last_infer_len
        )

    # ---- LLM 推断 ----
    def prompt(self, width: int = 41, height: int = 32) -> str:
        parts = [
            "=== 民间传闻（逐日累积）===\n" + "\n".join(self.legends[-12:]),
        ]
        if self.failed_sites:
            # 失败反馈（用户 IKIAE6 2026-09-24：召唤结果 2 = 地点/祭品不对）→ 换新地点重推
            bad = "、".join("(%d,%d)" % p for p in self.failed_sites[-5:])
            parts.append(
                f"=== 失败反馈 ===\n以下坐标已尝试召唤但**失败**：{bad}。"
                "请结合传闻**重新推断**，不要重复给出这些坐标。"
            )
        parts.append(
            "\n请据线索推断宝藏：祭坛坐标(x,y)、需献祭的任务用品(英文名)、开启天数。"
            f"地图为 {width}×{height}，坐标范围 x∈[0,{width - 1}]、y∈[0,{height - 1}]"
            "（越界坐标会被直接丢弃，务必给出界内整数）。"
            '只返回 JSON：{"x":<int>,"y":<int>,"items":["AcientTablet",...],"day":<int>,"ready":<bool>}。'
            "信息不足时 ready=false。可用用品：" + ", ".join(TREASURE_ITEMS)
        )
        return "\n".join(parts)

    def apply_llm(self, response: str) -> bool:
        obj = None
        t = (response or "").strip()
        for blob in [t, *_JSON_BLOCK.findall(t)]:
            try:
                parsed = json.loads(blob)
                if isinstance(parsed, dict):
                    obj = parsed
                    break
            except (json.JSONDecodeError, ValueError):
                continue
        if not obj:
            return False
        try:
            x, y = int(obj.get("x")), int(obj.get("y"))
        except (TypeError, ValueError):
            return False
        # 计划校验（策略书 §8.3）：坐标/祭品/开启日非法 → 丢弃计划（防 LLM 臆造）
        if not (0 <= x < 41 and 0 <= y < 32):
            return False
        items = tuple(
            str(i) for i in (obj.get("items") or []) if str(i) in TREASURE_ITEMS
        )
        if not items:
            return False
        try:
            day = int(obj.get("day") or 0)
        except (TypeError, ValueError):
            day = 0
        if not (1 <= day <= 10):
            return False
        ready = bool(obj.get("ready"))
        # 已失败过的坐标不再接受（用户 IKIAE6：结果 2 = 地点不对 → 必须换地点）
        if (x, y) in self.failed_sites:
            return False
        self.plan = TreasurePlan(Pos(x, y), items, day, ready, (response or "")[:200])
        return True

    def on_summon_result(self, code: int) -> None:
        """召唤结果（策略书 §8.4）：1/4=完成不再尝试；2/3=失败→记录失败点并**立即用全部传闻重推**。"""
        if code in (1, 4):
            self.attempted = True
        elif code in (2, 3):
            if self.plan.location is not None:
                site = (self.plan.location.x, self.plan.location.y)
                if site not in self.failed_sites:
                    self.failed_sites.append(site)
            self.plan = TreasurePlan()
            self.attempted = False
            # **立即重推**（不等新传闻）：把累计传闻 + 失败反馈一起再问 LLM
            self._last_infer_len = 0

    def record_attempt(self) -> None:
        self.attempted = True

    # ---- 行动 ----
    def cmd(self, turn: Turn, pioneer: Unit, shop: Pos | None, ctx) -> dict[str, Any] | None:
        """ready 计划才行动：备祭品 → 到祭坛 → summonTreasure。"""
        if not self.plan.ready or self.attempted or self.plan.location is None:
            return None
        loc = self.plan.location
        if self.is_failed_site(loc):     # 已失败过的坐标不再尝试（防重复空耗）
            self.plan = TreasurePlan()
            return None
        missing = [it for it in self.plan.items if it not in pioneer.backpack]
        if missing:
            if shop is None:
                return None
            item = missing[0]
            price = turn.shop_prices.get(item, 15)
            if turn.gold < price:
                return None
            if pioneer.pos != shop and distance(pioneer.pos, shop) <= 1:
                return buy_command(item, 1)
            step = step_toward(turn, pioneer, shop, ctx.reserved)
            return move_command(step) if step is not None else None
        # 未到开启日 → 先不召唤/不前往（用户 2026-09-24："回合数到了再尝试召唤"）；
        # 祭品可提前备好，白天回防回合由开拓者 FSM 的 must_return 预留。
        if self.plan.day and turn.day_index < self.plan.day:
            return None
        if distance(pioneer.pos, loc) <= 1:
            self.attempted = True
            return summon_treasure_command(loc, list(self.plan.items))
        step = step_toward(turn, pioneer, loc, ctx.reserved)
        return move_command(step) if step is not None else None