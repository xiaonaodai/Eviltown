"""单智能体最小网格 demo：跑一天，导出需求曲线。

用法:
    python -X utf8 demo/needs_demo.py [ticks=1440] [csv=out/needs_curve.csv]

说明:
- 复用项目内真实模块（Config / Simulation / Brain），不复制业务逻辑
- 人口设为 1，网格尽量小；落库关闭，只导出 CSV
"""
from __future__ import annotations

import csv
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eviltown.agents.sim import Simulation  # noqa: E402
from eviltown.config import Config, StorageCfg, TimeCfg, WorldCfg  # noqa: E402

WORLD_CANDIDATES = ((60, 40), (120, 80), (240, 160))
FIELDS = ["hunger", "bladder", "energy", "social"]


def build_cfg(w: int, h: int) -> Config:
    cfg = Config()
    cfg.population = 1
    cfg.world = WorldCfg(width=w, height=h)
    cfg.time = TimeCfg(ticks_per_day=1440, start_day=1, start_tick=360)
    cfg.storage = StorageCfg(db_path=None, record_events=False)  # type: ignore[arg-type]
    return cfg


def make_sim() -> tuple[Simulation, tuple[int, int]]:
    for w, h in WORLD_CANDIDATES:
        try:
            sim = Simulation(build_cfg(w, h))
        except Exception as exc:  # 小网格可能放不下城镇，逐级放大
            print(f"[warn] world {w}x{h} 不可用: {exc}")
            continue
        if sim.residents:
            return sim, (w, h)
    raise SystemExit("无法构造仿真世界")


def main() -> None:
    ticks = int(sys.argv[1]) if len(sys.argv) > 1 else 1440
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join("data", "needs_curve.csv")

    sim, (w, h) = make_sim()
    r = sim.residents[0]
    print(f"世界 {w}x{h} · 人口 {len(sim.residents)} · 居民 {r.name}（{r.job.value}）")

    rows = []
    for _ in range(ticks):
        sim.tick(1)
        rows.append([
            sim.clock.day, sim.clock.tick, sim.clock.clock_str,
            round(r.hunger, 2), round(r.bladder, 2), round(r.energy, 2),
            round(r.social, 2), round(r.mood, 2), r.action, r.x, r.y,
        ])

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["day", "tick", "clock", *FIELDS, "mood", "action", "x", "y"])
        wr.writerows(rows)

    print(f"\n已导出 {len(rows)} 行 → {out}")
    print(f"{'need':<9}{'min':>8}{'max':>8}{'mean':>8}")
    for i, name in enumerate(FIELDS, start=3):
        vals = [row[i] for row in rows]
        print(f"{name:<9}{min(vals):>8.1f}{max(vals):>8.1f}{sum(vals) / len(vals):>8.1f}")

    acts = Counter(row[8] for row in rows)
    print("\n行为占比:")
    for a, c in acts.most_common():
        print(f"  {a:<10}{c:>5} tick  {c / len(rows) * 100:5.1f}%")


if __name__ == "__main__":
    main()
