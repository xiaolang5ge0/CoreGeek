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

# 任务用品英文名（任务书 §4.6.3）——**仅作商店清单缺失时的兜底**（列表随地图刷出、不固定）
TREASURE_ITEMS = (
    "AcientTablet", "StarSand", "FlameBreath", "FrostPotion", "ThornAmulet", "IronWhistle",
)
# 任务书 §4.6.3 固定的**非任务用品**：升级券 6 种 + 消耗品/召唤令 8 种。
# 商店清单里不在本集合中的物品，即本张地图刷出的「任务用品」（祭品）。
NON_OFFERING = frozenset({
    "WeaponUpgradeVoucher1", "WeaponUpgradeVoucher2",
    "WallUpgradeVoucher1", "WallUpgradeVoucher2",
    "StationUpgradeVoucher1", "StationUpgradeVoucher2",
    "WallFixer", "Medicine", "DizzyWeapon", "Bomb",
    "SmallRobotSummonOrder", "MiddleRobotSummonOrder",
    "LargeRobotSummonOrder", "BossRobotSummonOrder",
})
_JSON_BLOCK = re.compile(r"\{.*\}", re.S)
_ZONE_LABEL = {
    "stone": "石矿", "iron": "铁矿", "copper": "铜矿",
    "vendor": "小贩", "weaponShop": "武器商店",
}


def offerings_from_shop(shop_names) -> tuple[str, ...]:
    """从武器商店清单**动态识别**本图任务用品（参考 IKIF4V / IKIEEC）。

    任务书 §4.6.3：任务用品列表随地图刷出、不固定 → 不能写死；商店清单里除固定商品
    （见 `NON_OFFERING`）外的物品即任务用品。清单缺失/为空时退回 `TREASURE_ITEMS` 兜底。
    """
    names = tuple(str(n) for n in (shop_names or ()) if str(n) not in NON_OFFERING)
    return names or TREASURE_ITEMS


def zones_text(turn: Turn) -> str:
    """中立元素（矿/小贩/商店/任务点）坐标清单——传闻里的方位词可据此锚定（参考 IKIF4V）。"""
    items: list[str] = []
    for pos, kind in sorted(turn.zones.items(), key=lambda kv: (kv[0].x, kv[0].y)):
        label = _ZONE_LABEL.get(kind)
        if label:
            items.append("%s(%d,%d)" % (label, pos.x, pos.y))
    for task in getattr(turn, "tasks", ()) or ():
        items.append("任务点(%d,%d)" % (task.pos.x, task.pos.y))
    return "；".join(items)


@dataclass
class TreasurePlan:
    location: Pos | None = None
    items: tuple[str, ...] = ()
    day: int = 0
    ready: bool = False
    raw: str = ""
    # IKKE6Q-Q3（用户采纳）：候选坐标列表（主选之后的备选，按 LLM 给出顺序）——
    # ts=2 依次换下一个候选召唤，全部用尽才重推 LLM（不再单点押注一次方向换算）
    candidates: tuple[Pos, ...] = ()

    def dump(self) -> dict:
        return {
            "location": self.location.dump() if self.location else None,
            "items": list(self.items),
            "day": self.day,
            "ready": self.ready,
            "candidates": [p.dump() for p in self.candidates],
        }


class TreasurePlanner:
    def __init__(self) -> None:
        self.legends: list[str] = []
        self.legend_days: list[int] = []   # 每条传闻到来的天数（prompt 标注用）
        self.plan = TreasurePlan()
        self.attempted = False
        self._last_infer_len = 0   # 上次推断时的线索条数（新线索到来才再问 LLM，用户 2026-09-23）
        self.failed_sites: list[tuple[int, int]] = []   # 召唤失败过的坐标（结果 2，需换地点重推）
        # P1-6（实战 F11/F12）：结果码 0=召唤未真正执行——旧版把 attempted 置 True 后
        # 遇 0 永不复位 → 宝藏链卡死；结果码 3=祭品错，地点可能正确——旧版误拉黑地点
        self.summon_tries: int = 0                    # 连续"结果 0"计数（上限 3 次后放弃重推）
        self.tried_items: list[tuple[int, int, tuple[str, ...]]] = []   # 结果 3 的（地点, 祭品组合）
        self._await_result: bool = False              # summon 已发出、待本回合结果码结账

    # ---- 线索累积 ----
    def observe(self, folk: str, day: int = 0) -> bool:
        text = (folk or "").strip()
        if not text or text in self.legends:
            return False
        self.legends.append(text)
        self.legend_days.append(int(day or 0))
        return True

    def mark_inferred(self) -> None:
        """记录"已就当前线索问过 LLM"，避免同一批线索反复提问。"""
        self._last_infer_len = len(self.legends)

    def is_failed_site(self, pos: Pos | None) -> bool:
        return pos is not None and (pos.x, pos.y) in self.failed_sites

    def needs_inference(self) -> bool:
        # 有新线索（条数增加）且尚未尝试召唤 → 再问 LLM。
        # 参考 IKIF4V：新传闻可能**补充/推翻**旧推理 → 即使已有 ready 计划也重推（用户 2026-09-23：
        # 不是一次不成就放弃；`attempted` 后不再重推）。
        return (
            bool(self.legends)
            and not self.attempted
            and len(self.legends) > self._last_infer_len
        )

    # ---- LLM 推断 ----
    def prompt(
        self,
        width: int = 41,
        height: int = 32,
        day: int = 1,
        zones: str = "",
        offerings=None,
    ) -> str:
        offerings = tuple(offerings or TREASURE_ITEMS)
        parts: list[str] = []
        if zones:
            # 中立元素坐标（参考 IKIF4V）：传闻里的「渡口/林场/矿区」等方位词可据此锚定
            parts.append("=== 地图中立元素坐标 ===\n" + zones)
        labeled = []
        for i, text in enumerate(self.legends):
            d = self.legend_days[i] if i < len(self.legend_days) else 0
            labeled.append("第%d天传闻：%s" % (d or i + 1, text))
        parts.append("=== 民间传闻（逐日累积）===\n" + "\n".join(labeled))
        if self.failed_sites:
            # 失败反馈（用户 IKIAE6 2026-09-24：召唤结果 2 = 地点/祭品不对）→ 换新地点重推
            bad = "、".join("(%d,%d)" % p for p in self.failed_sites[-5:])
            parts.append(
                f"=== 失败反馈 ===\n以下坐标已尝试召唤但**失败**：{bad}。"
                "请结合传闻**重新推断**，不要重复给出这些坐标。"
                "若这些坐标的推断涉及『之北/之南』等相对方位，**优先怀疑 y 轴方向此前算反**"
                "（正确换算见下：北=y+1、南=y-1）——先检查这个再换地点。"
            )
        if self.tried_items:
            # P1-6/P1-7：结果码 3=祭品错、地点可能正确——反馈已试组合，只换祭品不换地
            lines = ["(%d,%d) 祭品=%s" % (x, y, "+".join(items))
                     for x, y, items in self.tried_items[-5:]]
            parts.append(
                "=== 祭品已试组合（召唤结果 3：地点可能正确、**祭品组合错误**） ===\n"
                + "\n".join(lines)
                + "\n这些地点**可以保留**，但必须更换祭品组合："
                "核对祭品**数量**（『三道封印/门需三钥』= 3 个）与**种类拼写**（必须用本图任务用品英文名）。"
            )
        parts.append(
            "\n请据线索推断宝藏：祭坛坐标(x,y)、需献祭的任务用品(英文名)、开启天数。"
            f"地图为 {width}×{height}，坐标范围 x∈[0,{width - 1}]、y∈[0,{height - 1}]"
            "（越界坐标会被直接丢弃，务必给出界内整数）。"
            f"**当前是第 {day} 天**（整场 10 天）。\n"
            "=== 解读提示（务必按此推理，不要凭空给地图中部坐标）===\n"
            f"0. **地图坐标系（IKKDR0-F18 勘误，务必遵守）**：原点在**左下角 (0,0)**，"
            f"x 向右增大（**东**）、y 向上增大（**北**）。\n"
            "   相对方位换算：**之东 = x+1、之西 = x-1、之北 = y+1、之南 = y-1**"
            "（例：『总号(18,18)之东两格、之北一格』→ (18+2, 18+1) = (20,19)）。\n"
            "1. **绝对方位**：西部 → x 取**西侧小值**（x≤5）；"
            f"东部 → x 取东侧大值（x≥{width - 6}）；"
            f"**北部 → y 取北侧大值（y≥{height - 6}）**；**南部 → y 取南侧小值（y≤5）**。\n"
            "   只取与『**石门 / 石殿 / 祭坛**』**同一句**的那个方位；其它场景"
            "（渡口 / 林场 / 矿区 / 集市 / 狼嚎）提到的方位与数字是**背景干扰**，**不要**用于定位。\n"
            "   若只给出一个方位，另一轴取与它同侧的地图角（例如『西部』→ 取西侧一角，"
            "而不是地图中部）。\n"
            "2. 『石门 / 石殿 / 祭坛』就是**召唤点**；『门需三钥 / 三道杠 / 三道封印 / 刮了三次』等"
            "数字 → **祭品数量**。\n"
            "3. 祭品英文名对应传闻里的『稀奇玩意儿 / 封印之物』："
            "铭文石板→AcientTablet；不灭之光·光之尘→StarSand；纯净之火·橙红雾→FlameBreath。\n"
            "4. 开启天数：若传闻无硬性线索，给一个你认为最可能的 **1~10** 整数，"
            f"且**不得早于当前天数（第 {day} 天）**（早于今天的计划无效）。\n"
            "5. 先在 `reason` 里写清推断依据；为便于日志查看，**`reason` 放在 JSON 最后**。\n"
            '只返回 JSON：{"x":<int>,"y":<int>,"items":["AcientTablet",...],"day":<int>,"ready":<bool>,'
            '"candidates":[{"x":<int>,"y":<int>}],"reason":"<推断依据>"}。'
            "candidates=**备选坐标**（主选之外最有可能的 1-2 个，按可能性排序、可为空数组）——"
            "主选召唤失败会依次尝试备选，不必重复推断。\n"
            "信息不足时 ready=false。可用用品（**本图**任务用品）：" + ", ".join(offerings)
        )
        return "\n".join(parts)

    def apply_llm(self, response: str, current_day: int = 0, offerings=None) -> bool:
        offerings = tuple(offerings or TREASURE_ITEMS)
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
        # 计划校验（策略书 §8.3 + 参考文档 IKIEEC 的 6 道验证）：坐标/祭品/开启日非法 → 丢弃
        if not (0 <= x < 41 and 0 <= y < 32):
            return False
        items = tuple(
            str(i) for i in (obj.get("items") or []) if str(i) in offerings
        )
        if not items:
            return False
        try:
            day = int(obj.get("day") or 0)
        except (TypeError, ValueError):
            day = 0
        if not (1 <= day <= 10):
            return False
        # 第 6 道验证（IKIEEC）：**开启日不得早于当前天数**（否则永远打不开 → 直接丢弃、重推）
        if current_day and day < current_day:
            return False
        ready = bool(obj.get("ready"))
        # 已失败过的坐标不再接受（用户 IKIAE6：结果 2 = 地点不对 → 必须换地点）
        if (x, y) in self.failed_sites:
            return False
        # IKKE6Q-Q3（用户采纳）：解析候选列表（主选之后的备选，最多 2 个，界内、未失败）
        cands = []
        for c in (obj.get("candidates") or [])[:2]:
            try:
                cx, cy = int(c.get("x")), int(c.get("y"))
            except (TypeError, ValueError, AttributeError):
                continue
            if (0 <= cx < 41 and 0 <= cy < 32) and (cx, cy) not in self.failed_sites \
                    and (cx, cy) != (x, y):
                cands.append(Pos(cx, cy))
        self.plan = TreasurePlan(Pos(x, y), items, day, ready, (response or "")[:200],
                                 candidates=tuple(cands))
        return True

    def mark_summon_sent(self) -> None:
        """summon 指令已发出 → 本回合结果码必须结账（P1-6：0 也是有效结果）。"""
        self._await_result = True

    def take_await(self) -> bool:
        """取走并清零"待结账"标志：brain 据此决定 0 结果码是否需要消费。"""
        awaiting = self._await_result
        self._await_result = False
        return awaiting

    def on_summon_result(self, code: int) -> None:
        """召唤结果（策略书 §8.4 + P1-6 修订）：
        1/4=完成不再尝试；2=地点错→记失败点换地重推；
        3=祭品错→**保留地点**记失败组合重推；0=召唤未真正执行→保留计划重试（≤3 次）。"""
        if code in (1, 4):
            self.summon_tries = 0
            self.attempted = True
        elif code == 0:
            # 召唤没真正执行（指令被顶替/动作非法）：计划未被检验，原样保留重试；
            # 连续 3 次仍 0 → 视为死链，放弃本次计划并触发重推
            self.summon_tries += 1
            if self.summon_tries >= 3:
                self.plan = TreasurePlan()
                self.summon_tries = 0
                self._last_infer_len = 0
        elif code in (2, 3):
            self.summon_tries = 0
            if code == 2:
                # 地点错：记失败地点
                if self.plan.location is not None:
                    site = (self.plan.location.x, self.plan.location.y)
                    if site not in self.failed_sites:
                        self.failed_sites.append(site)
                # IKKE6Q-Q3（用户采纳）：还有候选坐标 → 依次切换（保 items/day 不动），
                # 全部用尽才作废计划重推 LLM
                if self.plan.candidates:
                    nxt = self.plan.candidates[0]
                    self.plan = TreasurePlan(
                        location=nxt, items=self.plan.items, day=self.plan.day,
                        ready=self.plan.ready, raw=self.plan.raw,
                        candidates=self.plan.candidates[1:])
                    self.attempted = False
                    return
                self.plan = TreasurePlan()
            else:
                # 祭品错：地点可能正确（文档 §1.1 结果码语义）→ 保留地点，
                # 记失败祭品组合，重推时反馈 tried-items 让 LLM 换组合
                if self.plan.location is not None:
                    combo = (self.plan.location.x, self.plan.location.y,
                             tuple(self.plan.items))
                    if combo not in self.tried_items:
                        self.tried_items.append(combo)
                self.plan = TreasurePlan(location=self.plan.location)
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
        # 提前 1 天移动到祭坛（参考文档 IKIEEC：确保开启日当天已在祭坛旁，不浪费赶路回合）；
        # 祭品可提前备好，白天回防回合由开拓者 FSM 的 must_return 预留。
        if self.plan.day and turn.day_index < self.plan.day - 1:
            return None
        if distance(pioneer.pos, loc) <= 1:
            if self.plan.day and turn.day_index < self.plan.day:
                return None      # 已到祭坛旁但未到开启日 → 原地待命（回防交给 FSM）
            # P1-6：不再提前置 attempted=True（结果码 0 时会永久卡死）；
            # 改记"待结账"，由下回合结果码决定：0→重试，1/4→完成，2/3→重推
            self.mark_summon_sent()
            return summon_treasure_command(loc, list(self.plan.items))
        step = step_toward(turn, pioneer, loc, ctx.reserved)
        return move_command(step) if step is not None else None