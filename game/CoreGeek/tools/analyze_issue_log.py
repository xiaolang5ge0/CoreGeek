#!/usr/bin/env python3
"""分析实战遥测日志：解密 round_record 并按“自进化任务书”交互整理成 UTF-8 报告。

用途（供后续迭代进化复用）：
  - 输入可以是 GitHub issue 编号（自动拉取 issue 正文）或本地日志文件；
  - 自动定位所有 `round_record <b64>`，按 telemetry 同款密钥流解密；
  - 只保留与自进化任务相关的回合（phaseTask / executeCmd / LLM / 命令结果 / 任务命令）；
  - 结果写入 UTF-8 文件（避免 Windows 控制台 GBK 乱码），并打印统计摘要。

用法：
  py tools/analyze_issue_log.py --issue 18 19
  py tools/analyze_issue_log.py --file logs/match_20260922_061319.jsonl.enc
  py tools/analyze_issue_log.py --issue 18 --out task_report.txt

说明：
  - 仅标准库，Python >= 3.11；
  - 只读分析，不修改任何决策代码；
  - 若某条记录在 issue 正文里被截断（JSON 不完整），会标记 [truncated] 并跳过，不影响其它回合。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import urllib.request

LOG_KEY = b"12345678"  # 与 src/agent/telemetry.py 保持一致

TASK_ACTIONS = {"acceptTask", "executeCmd", "submitAnswer", "answerTask", "abandonTask"}
# 报告中输出的字段（telemetry.compact_record 的键）
KEEP_KEYS = ("r", "g", "sc", "pt", "llm", "xcmd", "lcr", "fb_fail", "err")


def _keystream(n: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(LOG_KEY + counter.to_bytes(4, "big")).digest()
        counter += 1
    return out[:n]


def decrypt_text(b64: str) -> str:
    data = base64.b64decode(b64)
    ks = _keystream(len(data))
    return bytes(a ^ b for a, b in zip(data, ks)).decode("utf-8", "replace")


def fetch_issue_body(repo: str, number: int, timeout: int = 60) -> tuple[str, str]:
    """返回 (标题, 正文)。"""
    url = f"https://api.github.com/repos/{repo}/issues/{number}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "CoreGeek-log-analyzer", "Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    return data.get("title") or f"issue-{number}", data.get("body") or ""


def fetch_issue_comments(repo: str, number: int, timeout: int = 60) -> list[str]:
    """返回 issue 全部评论正文（遥测常因正文 60KB 截断，续在评论里）。"""
    url = f"https://api.github.com/repos/{repo}/issues/{number}/comments?per_page=100"
    req = urllib.request.Request(
        url, headers={"User-Agent": "CoreGeek-log-analyzer", "Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    return [str(c.get("body") or "") for c in data]


def parse_records(text: str) -> list[dict]:
    """从文本中提取并解密所有 round_record；无法解析的以 {'_error': ...} 返回。"""
    records: list[dict] = []
    for match in re.finditer(r"round_record ([A-Za-z0-9+/=]+)", text):
        blob = match.group(1)
        try:
            records.append(json.loads(decrypt_text(blob)))
        except Exception as exc:  # noqa: BLE001 - 记录失败原因即可
            records.append({"_error": str(exc), "_blob_len": len(blob)})
    return records


def is_task_round(rec: dict) -> bool:
    """是否与自进化任务交互相关。"""
    if any(k in rec for k in ("pt", "llm", "xcmd", "lcr")):
        return True
    for value in (rec.get("c") or {}).values():
        if isinstance(value, list) and value and value[0] in TASK_ACTIONS:
            return True
    return False


def format_round(rec: dict) -> str:
    r = rec.get("r")
    lines = [f"--- 回合 r={r}  gold={rec.get('g')} score={rec.get('sc')} ---"]
    if "pt" in rec:
        lines.append(f"  任务书(phaseTask): {rec['pt']}")
    for key, value in (rec.get("c") or {}).items():
        lines.append(f"  指令[{key}]: {json.dumps(value, ensure_ascii=False)}")
    if "xcmd" in rec:
        lines.append(f"  executeCmd: {rec['xcmd']}")
    if "llm" in rec:
        lines.append(f"  LLM回复: {rec['llm']}")
    if "lcr" in rec:
        lines.append(f"  上轮结果: {rec['lcr']}")
    if "fb_fail" in rec:
        lines.append(f"  失败动作: {rec['fb_fail']}")
    if "err" in rec:
        lines.append(f"  错误: {rec['err']}")
    return "\n".join(lines)


def build_report(source_label: str, records: list[dict]) -> str:
    good = [r for r in records if "_error" not in r]
    bad = [r for r in records if "_error" in r]
    parts = [
        "#" * 90,
        f"# {source_label}   (decrypted rounds: {len(good)}/{len(records)}, task rounds below)",
        "#" * 90,
    ]
    for rec in good:
        if is_task_round(rec):
            parts.append(format_round(rec))
    if bad:
        parts.append("")
        parts.append(f"[!] {len(bad)} 条记录解密后 JSON 不完整（通常是在 issue 正文中被截断），已跳过：")
        for rec in bad:
            parts.append(f"    - {rec['_error']} (b64_len={rec.get('_blob_len')})")
    return "\n".join(parts) + "\n"


def collect_texts(args: argparse.Namespace) -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    for number in args.issue or []:
        title, body = fetch_issue_body(args.repo, number)
        # 正文常被 60KB 截断 → 合并全部评论（遥测续写）
        chunks = [body, *fetch_issue_comments(args.repo, number)]
        texts.append((f"ISSUE #{number}: {title}", "\n".join(chunks)))
    for path in args.file or []:
        with open(path, encoding="utf-8", errors="ignore") as handle:
            texts.append((f"FILE: {path}", handle.read()))
    return texts


def main() -> int:
    parser = argparse.ArgumentParser(description="解密并整理自进化任务交互日志")
    parser.add_argument("--issue", type=int, nargs="*", help="GitHub issue 编号（可多个）")
    parser.add_argument("--file", nargs="*", help="本地日志文件（可多个）")
    parser.add_argument("--repo", default="xiaolang5ge0/CoreGeek", help="GitHub 仓库 owner/name")
    parser.add_argument("--out", default="task_report.txt", help="输出 UTF-8 报告路径")
    args = parser.parse_args()

    if not args.issue and not args.file:
        parser.error("至少提供 --issue 或 --file 之一")

    texts = collect_texts(args)
    if not texts:
        print("没有可分析的数据源", file=sys.stderr)
        return 1

    report = "\n".join(build_report(label, parse_records(body)) for label, body in texts)
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(report)

    print(f"[ok] 报告已写入 {args.out}（{len(report)} 字符，UTF-8）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
