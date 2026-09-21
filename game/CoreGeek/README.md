# CoreGeek《未来战争》Agent

分层架构详见 `CompetitionTopic/ARCHITECTURE_DESIGN.md`，策略真源见 `CompetitionTopic/STRATEGY_DECISIONS.md`。

## 运行

```bash
python main3.py <port>     # 平台加载方式（主办方扫描 main3.py）
bash run.sh <port>         # 兜底
```

## 测试

```bash
cd game/CoreGeek
python -m unittest discover -s tests -v
```

## 打包

```bash
python tools/build_package.py   # → dist/CoreGeek.tar.gz（tar 根层含 main3.py/run.sh/src/）
```

## 约束

- Python ≥ 3.11，**仅用标准库**（对战沙盒无第三方包）。
- 遥测：每回合 request/response/trace 落盘 `logs/match_*.jsonl`。
