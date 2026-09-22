"""L3 新闻经济：解析官方消息的矿种/停工/天数，预测最优售卖时机。

第一层（正则）：提取"铜/铁/石" + "停工/塌方/检修" + 天数 → 维护 (事件, 日期, 资源) 映射。
第二层（LLM）：可选，把新闻送 LLM 评估（本模块只做确定性解析，LLM 由 task 求解器顺带处理）。

策略：
- 停工前：继续采该矿种囤货（should_stockpile）
- 停工期：优先卖该矿种（高价，boost）
- 恢复后/官方说"恢复"：清空预测，恢复默认
"""
from __future__ import annotations

import re

ORE_WORDS = {
    "铜": "copper", "铁": "iron", "石": "stone",
    "copper": "copper", "iron": "iron", "stone": "stone",
}
STOP_WORDS = ("停工", "停产", "塌方", "检修", "事故", "封闭", "关闭", "抢修", "加固", "受损")
RESUME_WORDS = ("恢复", "复产", "复工", "重新开采", "恢复开采", "重新运作")
CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7}


class NewsEconomy:
    def __init__(self) -> None:
        self.predictions: dict[str, dict] = {}  # ore -> {"start_day","days","reason"}
        self._seen: set = set()

    def update(self, official_news: str, day: int) -> None:
        text = (official_news or "").strip()
        if not text or text in self._seen:
            return
        self._seen.add(text)
        ore = self._parse_ore(text)
        if ore is None:
            return
        if any(w in text for w in RESUME_WORDS):  # 恢复 → 清空预测
            self.predictions.pop(ore, None)
            return
        if any(w in text for w in STOP_WORDS):
            self.predictions[ore] = {
                "start_day": day + 1,          # 官方多为"明天+后天"停工
                "days": self._parse_days(text),
                "reason": text[:40],
            }

    @staticmethod
    def _parse_ore(text: str) -> str | None:
        for word, ore in ORE_WORDS.items():
            if word in text:
                return ore
        return None

    @staticmethod
    def _parse_days(text: str) -> int:
        m = re.search(r"(\d+)\s*天", text)
        if m:
            return max(1, min(10, int(m.group(1))))
        m = re.search(r"([一二两三四五六七])\s*天", text)
        if m:
            return CN_NUM.get(m.group(1), 2)
        return 2  # 默认 2 天

    def boost(self, ore: str, day: int) -> float:
        """该矿种当前是否应优先售卖（停工期/涨价期）→ 返回售卖加权。"""
        p = self.predictions.get(ore)
        if not p:
            return 0.0
        start, days = p["start_day"], p["days"]
        return 50.0 if start <= day < start + days else 0.0

    def should_stockpile(self, ore: str, day: int) -> bool:
        """停工前：继续采该矿种囤货。"""
        p = self.predictions.get(ore)
        return bool(p and day < p["start_day"])

    def boosts(self, day: int) -> dict:
        return {ore: self.boost(ore, day) for ore in self.predictions}
