"""EvilTown 最小回归测试（不依赖 LLM、不依赖 SQLite）。

覆盖项目关键约定：
- 时间：1 tick = 1 仿真分钟；ticks_per_day=1440；start_tick=360（06:00 开局）
- 需求衰减：bladder 每 tick +0.13（无夜间/睡眠修正，最稳定）
- 仿真可无 DB 运行；居民不越界、不死亡
- snapshot 结构稳定
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from eviltown.agents.sim import DECAY, Simulation
from eviltown.world.clock import Clock


def make_cfg(population: int = 20, w: int = 120, h: int = 80) -> SimpleNamespace:
    """最小配置：storage=None 关掉落库；字段与 config.py 结构对齐。"""
    return SimpleNamespace(
        population=population,
        world=SimpleNamespace(width=w, height=h),
        time=SimpleNamespace(
            ticks_per_day=1440, start_day=1, start_tick=360, speed=1
        ),
        storage=None,
    )


# ------------------------------------------------------------------ 时钟
def test_clock_day_rollover():
    c = Clock(ticks_per_day=1440, start_day=1, start_tick=360)
    c.advance(1440)
    assert c.day == 2
    assert c.tick == 360  # 06:00 又是新的一天


def test_clock_derived_fields():
    c = Clock(start_tick=360)
    assert c.hour == 6 and c.minute == 0
    assert c.clock_str == "06:00"
    assert not c.is_night
    c.tick = 22 * 60
    assert c.is_night
    c.tick = 10 * 60          # 10:00 → 上班时间且上学时间
    assert c.is_work_hours and c.is_school_hours
    c.tick = 18 * 60          # 18:00 → 两者都结束
    assert not c.is_work_hours and not c.is_school_hours
    c.tick = 8 * 60           # 08:00 → 仅上学时间
    assert not c.is_work_hours and c.is_school_hours


# ------------------------------------------------------------------ 需求衰减
def test_bladder_decay_one_tick():
    sim = Simulation(make_cfg())
    before = [r.bladder for r in sim.residents]
    sim.tick(1)
    for b0, r in zip(before, sim.residents):
        # bladder 无夜间/睡眠修正，1 tick 恰好 +0.13
        assert r.bladder == pytest.approx(b0 + DECAY["bladder"])


def test_decay_table_matches_design():
    # 与 AGENTS.md / DESIGN.md 记录值一致
    assert DECAY == {"hunger": 0.09, "bladder": 0.13, "energy": 0.06, "social": 0.03}


# ------------------------------------------------------------------ 仿真整体
def test_simulation_runs_without_db():
    sim = Simulation(make_cfg())
    assert sim._con is None  # storage=None 时不建库连接
    assert len(sim.residents) == 20

    for _ in range(200):
        sim.tick(1)

    for r in sim.residents:
        assert 0 <= r.x < sim.grid.w
        assert 0 <= r.y < sim.grid.h
        assert r.alive is True
    assert len(sim.residents) == 20


def test_snapshot_shape():
    sim = Simulation(make_cfg())
    sim.tick(5)
    s = sim.snapshot()
    for key in ("clock", "paused", "residents", "stats", "fields", "relations", "events"):
        assert key in s
    assert len(s["residents"]) == 20
    assert s["stats"]["population"] == 20


def test_town_has_buildings():
    sim = Simulation(make_cfg())
    assert len(sim.buildings) > 0


# ------------------------------------------------------------------ 固定住宅
def test_every_resident_has_fixed_home():
    """每个居民都拥有一个固定的家：家是真实住宅，且有一张自己的床位。"""
    from eviltown.world.buildings import BuildingKind

    sim = Simulation(make_cfg())
    homes = {b.bid: b for b in sim.buildings if b.kind == BuildingKind.HOME}
    assert homes, "城镇应至少有一栋住宅"

    for r in sim.residents:
        assert r.home_id in homes, f"r{r.rid} 的家不是住宅"
        assert r.home_tile is not None, f"r{r.rid} 没有床位"
        assert homes[r.home_id].contains(*r.home_tile), \
            f"r{r.rid} 的床位 {r.home_tile} 不在自家屋内"
        assert sim.grid.walkable(*r.home_tile), f"r{r.rid} 的床位不可走"
        # 出生即在自己那张床上
        assert (r.x, r.y) == r.home_tile


def test_beds_do_not_overlap_within_a_home():
    """同一栋住宅内，各住户的床位互不重叠；且分配被打散（非 i % n 顺序）。"""
    from collections import defaultdict
    from eviltown.world.buildings import BuildingKind

    sim = Simulation(make_cfg())
    beds = defaultdict(list)
    for r in sim.residents:
        beds[r.home_id].append(r.home_tile)
    for bid, tiles in beds.items():
        assert len(tiles) == len(set(tiles)), f"住宅 {bid} 内床位重叠"

    # 相邻 rid 扎堆同一栋 = 分配未打散（旧实现 homes[i % n] 会全扎堆）
    adjacent = sum(1 for a, b in zip(sim.residents, sim.residents[1:])
                   if a.home_id == b.home_id)
    assert adjacent < len(sim.residents) // 2, "住宅分配过于集中，未打散"

    # 住宅分布应覆盖多栋，而不是全场挤一两栋
    assert len(beds) >= 3, f"只用到 {len(beds)} 栋住宅"


def test_night_residents_sleep_in_own_home():
    """入夜后大部分人回到自家屋内睡觉（而非在外游荡或在别人家）。"""
    sim = Simulation(make_cfg())
    # 06:00 → 次日 01:00（1140 tick），跨过 21:00 的入夜
    sim.tick(19 * 60)

    at_home = sum(1 for r in sim.residents if sim._at_home(r))
    assert at_home >= len(sim.residents) * 0.6, \
        f"夜间仅 {at_home}/{len(sim.residents)} 人在自己家里"


# ------------------------------------------------------------------ 行动状态机
def test_snapshot_has_moving_flag():
    """居民快照带 moving 布尔：路径非空=在途，供 UI 区分「在路上」与「已到达做事」。"""
    sim = Simulation(make_cfg())
    sim.tick(3)
    s = sim.snapshot()
    for r in s["residents"]:
        assert "moving" in r and isinstance(r["moving"], bool)
        # moving 必须与「是否还有路径」一致
        resident = sim.residents[r["id"]]
        assert r["moving"] == bool(resident.path)


def test_on_duty_worker_does_not_wander():
    """在岗的工人（工作时段身处工位）不应选择社交/购物/闲逛——治「上班摸鱼」。"""
    import random

    from eviltown.agents.brain import ActionType, decide
    from eviltown.world.buildings import BuildingKind

    sim = Simulation(make_cfg())
    # 2026-11-10 起职业改成「自己找的」（出生一律待业），这里直接指派一个雇主，
    # 只为验证「在岗不摸鱼」这条规则本身，不依赖出生配额。
    work = next(b for b in sim.buildings if b.kind == BuildingKind.OFFICE)
    worker = next(r for r in sim.residents if r.age >= 22)
    worker.work_id, worker.job_tile = work.bid, None
    # 把工人放到工位内，进入「在岗」状态
    worker.x, worker.y = work.entrance

    act = decide(worker, sim.buildings, hour=10, is_night=False,
                 rng=random.Random(0), at_work=True)
    assert act.kind not in (ActionType.SOCIALIZE, ActionType.SHOP, ActionType.WANDER), \
        f"在岗工人竟选择了 {act.kind}"

    # 对照：同样条件下不传在岗标记，闲逛/社交是可能的候选（不强制，仅保证不崩）
    decide(worker, sim.buildings, hour=10, is_night=False, rng=random.Random(0))


# ------------------------------------------------------------------ 需求活锁（阶段 1 修复）
def test_apply_settles_needs_on_arrival():
    """到达即结算：吃饭把饥饿清到低位、如厕把膀胱清零。"""
    from eviltown.agents.brain import ActionType

    sim = Simulation(make_cfg())
    r = sim.residents[0]

    r.hunger, r.action = 80.0, ActionType.EAT.value
    sim._apply(r)
    assert r.hunger <= 5.0, f"到店后饥饿仍为 {r.hunger}"

    # 如厕改为「触碰马桶并停留满 1 分钟」才结算（用户规格）。
    tb = next(b for b in sim.buildings if b.toilets)
    tile = tb.toilets[0]
    r.bladder, r.action = 70.0, ActionType.TOILET.value
    r.x, r.y = tile
    r.toilet_tile = tile
    sim.toilet_use[tile] = r.rid
    r.dwell = 1                        # 已停留满 1 分钟
    sim._apply(r)
    assert r.bladder == 0.0, f"站上马桶停留 1 分钟后膀胱仍为 {r.bladder}"

    # 未真正站到马桶格上（仍在排队等待）时不应清零。
    r2 = sim.residents[1]
    r2.bladder, r2.action = 70.0, ActionType.TOILET.value
    r2.x, r2.y = tb.entrance
    r2.toilet_tile = None
    r2.dwell = 5
    sim._apply(r2)
    assert r2.bladder > 60.0, "排队中不应清空膀胱"


def test_needs_are_relieved_not_livelocked():
    """需求活锁回归：推进一天，必须有人把饥饿降到低位（不再「只涨不落」）。"""
    sim = Simulation(make_cfg())
    lowest = min(r.hunger for r in sim.residents)
    for _ in range(12 * 60):
        sim.tick(1)
        lowest = min(lowest, min(r.hunger for r in sim.residents))
    assert lowest <= 5.0, "一整天无人吃饱——需求活锁仍存在"


def test_watchdog_does_not_interrupt_travel():
    """看门狗只在真卡死时触发：正常行走（有路径）时 stuck 不累加。"""
    sim = Simulation(make_cfg())
    r = sim.residents[0]
    b = next(b for b in sim.buildings if b.bid == r.home_id)
    # 造一段远距离行走：起点在自家、目标在另一头
    r.x, r.y = b.entrance
    tx, ty = min(sim.grid.w - 3, r.x + 30), r.y
    while not sim.grid.walkable(tx, ty) and tx > r.x:
        tx -= 1
    r.path = sim._path_to(r, tx, ty)
    r.target_id = b.bid
    sim._target_xy[r.rid] = (tx, ty)
    if not r.path:
        return  # 该地图/起点不构成远行程，跳过
    # 走 8 tick，看门狗不应把 stuck 顶到阈值（旧实现第 8 tick 必触发）
    for _ in range(8):
        if not r.path:
            break
        sim.tick(1)
    assert r.stuck < 8, f"正常行走被看门狗误判为卡死（stuck={r.stuck}）"


# ------------------------------------------------------------------ 配置
def test_config_expands_env_placeholder(monkeypatch, tmp_path):
    """llm.api_key 支持 ${VAR} / ${VAR:-默认} 环境变量引用（未设置时回落默认值）。"""
    from eviltown.config import load_config

    cfg = tmp_path / "c.yaml"
    cfg.write_text('llm:\n  api_key: "${EVILTOWN_TEST_KEY:-fallback-key}"\n', encoding="utf-8")

    monkeypatch.delenv("EVILTOWN_TEST_KEY", raising=False)
    assert load_config(cfg).llm.api_key == "fallback-key"

    monkeypatch.setenv("EVILTOWN_TEST_KEY", "sk-real-key")
    assert load_config(cfg).llm.api_key == "sk-real-key"


def test_config_missing_file_falls_back_to_defaults(tmp_path):
    """配置文件不存在时不报错，走 dataclass 默认值。"""
    from eviltown.config import load_config

    assert load_config(tmp_path / "nope.yaml").llm.api_key == ""
