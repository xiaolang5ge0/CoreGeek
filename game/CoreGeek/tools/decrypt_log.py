#!/usr/bin/env python3
"""解密遥测日志：从平台 stdout 或 logs/*.jsonl.enc 提取 round_record 行并解密。

用法：
  python tools/decrypt_log.py <日志文件>            # 输出解密后的 JSONL
  python tools/decrypt_log.py <日志文件> --pretty 5 # 美化输出第 5 回合
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sys

LOG_KEY = b"12345678"


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
    return bytes(a ^ b for a, b in zip(data, ks)).decode("utf-8")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: decrypt_log.py <logfile> [--pretty ROUND]")
    path = sys.argv[1]
    pretty_round = None
    if "--pretty" in sys.argv:
        pretty_round = int(sys.argv[sys.argv.index("--pretty") + 1])
    text = open(path, encoding="utf-8", errors="ignore").read()
    for m in re.finditer(r"round_record ([A-Za-z0-9+/=]+)", text):
        try:
            rec = json.loads(decrypt_text(m.group(1)))
        except Exception:
            continue
        if pretty_round is not None:
            if rec.get("r") == pretty_round:
                print(json.dumps(rec, ensure_ascii=False, indent=1))
        else:
            print(json.dumps(rec, ensure_ascii=False))
    if pretty_round is None:
        return
    # 文件本身可能就是 jsonl.enc（无 round_record 前缀）
    if not re.search(r"round_record", text):
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(decrypt_text(line))
            except Exception:
                continue
            if rec.get("r") == pretty_round:
                print(json.dumps(rec, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
