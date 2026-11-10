"""临时抽检：深夜入睡比例。"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_eviltown import make_cfg  # noqa: E402
from eviltown.agents.sim import Simulation  # noqa: E402


def main() -> None:
    sim = Simulation(make_cfg(population=30))
    # 推进到 02:00 附近（start 06:00 → 次日 02:00 共 1200 tick）
    for _ in range(1200):
        sim.tick(1)
    acts = Counter(r.action for r in sim.residents)
    n = len(sim.residents)
    print("time:", sim.clock.clock_str, "night:", sim.clock.is_night)
    print("actions:", dict(acts))
    print(f"sleeping {acts.get('sleep', 0)}/{n}")


if __name__ == "__main__":
    main()
