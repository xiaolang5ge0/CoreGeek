"""全局策略开关（训练期 vs 比赛收尾）。

HARDCODED_ASSIST —— 硬编码确定性能力总开关，**默认关闭**：

- False（默认，训练/自测期）：**不使用任何硬编码能力**，只用通用能力
  （健壮探索 + 命令锚定 + LLM-JSON 协议 + SOP），任务求解全部交给 LLM 驱动，
  以便真实衡量"自进化成功率"并据此训练提升。
  被禁用的硬编码路径：
    · API harvester（认证×参数矩阵 + 分页 + `__ANSWER` 自动组答 + 城市拼音映射）
    · 工程类 `./check` 探测（含 CRLF 自动修复）与 `[FAIL] DIR/LINE` 自动修复
    · `TOKEN:` / `__ANSWER` 输出自动提取提交
- True（比赛收尾，用户届时开启）：确定性优先 + LLM 兜底（硬编码 + LLM 双保障）。

切换方式：把下面常量改为 True（或设环境变量 COREGEEK_HARDCODED=1）。
"""
from __future__ import annotations

import os

HARDCODED_ASSIST: bool = os.environ.get("COREGEEK_HARDCODED", "0") == "1"
