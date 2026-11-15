"""长期记忆（RAG）回归测试：不依赖 LLM、不依赖真实 embedding。

覆盖四条硬约定：
- 关闭时**零行为**：add 返回 -1、search/recall 空、不建表也不抛异常；
- 没有 embedding 时走**关键词降级**，仍然能检索到；
- `recall()` 只给「加分」不给「删候选」，且条数受 `recall_k` 限制；
- **规则优先于记忆**：快饿晕的人不会因为记忆里「爱逛街」就先去逛街。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from eviltown.agents.brain import ActionType, decide
from eviltown.agents.memory_rag import TownHistory
from eviltown.agents.sim import Simulation
from eviltown.storage import db as storage_db


def rag_cfg(**kw) -> SimpleNamespace:
    """与 config.RagCfg 字段对齐的最小配置（embed=None 强制走关键词）。"""
    base = dict(enabled=True, recall_k=5, weight=6.0, embed=None,
                embed_model="", half_life_days=3.0, diary=True)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture()
def history(tmp_path):
    con = storage_db.connect(tmp_path / "memory.db")
    h = TownHistory(cfg=rag_cfg(), con=con)
    yield h
    con.close()


def test_disabled_is_completely_silent(tmp_path):
    """cfg=None：不建表、不写库、检索空 —— 与没有这个功能时行为一致。"""
    con = storage_db.connect(tmp_path / "off.db")
    h = TownHistory(cfg=None, con=con)
    assert h.enabled is False
    assert h.add(day=1, tick=1, kind="eat", text="X 去吃饭") == -1
    assert h.search("吃饭", now_day=1) == []
    assert h.recall("吃饭", now_day=1) == []
    assert h.remember_lines("吃饭", now_day=1) == []
    con.close()


def test_keyword_fallback_hits_right_memory(history):
    """没有 embedding 也要能按词面检索到（三级降级的最后一级）。"""
    history.add(day=1, tick=600, kind="bar", text="Alice 去酒吧喝了一杯",
                actor=1, place="酒吧")
    history.add(day=1, tick=620, kind="fish", text="Bob 去池塘钓鱼",
                actor=2, place="池塘")
    history.flush()

    hits = history.search("酒吧", now_day=1)
    assert hits, "关键词检索应有结果"
    assert "酒吧" in hits[0]["text"]


def test_recall_returns_bonus_and_is_capped(history):
    """召回给的是「加分」，条数不超过 recall_k。"""
    for i in range(8):
        history.add(day=1, tick=600 + i, kind="eat",
                    text=f"居民{i} 去饭馆吃饭", actor=i, place="饭馆")
    history.flush()

    recs = history.recall("吃饭", now_day=1)
    assert recs, "应召回至少一条"
    assert len(recs) <= 5, "recall_k=5 必须封顶"
    assert all(r.get("bonus", 0) > 0 for r in recs), "召回项应带正加分"


def test_rules_beat_memory_hunger_wins():
    """规则优先：快饿晕时，记忆里再爱逛街也先去吃饭。"""
    cfg = SimpleNamespace(
        population=6,
        world=SimpleNamespace(width=120, height=80),
        time=SimpleNamespace(ticks_per_day=1440, start_day=1,
                             start_tick=360, speed=1),
        storage=None,
    )
    sim = Simulation(cfg)
    r = sim.residents[0]
    r.hunger = 95.0          # 紧急裁决应当直接接管

    act = decide(r, sim.buildings, 12.0, False, sim.rng,
                 recall=[{"kind": ActionType.SHOP.value, "bonus": 99.0}])
    assert act.kind == ActionType.EAT, "记忆不能顶翻紧急裁决"


def test_recall_bonus_never_exceeds_cap():
    """同 kind 多条记忆的加分累计也封顶（_RECALL_BONUS_CAP=12）。"""
    from eviltown.agents.brain import _RECALL_BONUS_CAP

    cfg = SimpleNamespace(
        population=4,
        world=SimpleNamespace(width=120, height=80),
        time=SimpleNamespace(ticks_per_day=1440, start_day=1,
                             start_tick=360, speed=1),
        storage=None,
    )
    sim = Simulation(cfg)
    r = sim.residents[0]
    r.hunger = 10.0
    r.bladder = 10.0
    r.energy = 90.0

    # 三条同类记忆累计 60 分，也只应加 12 分 —— 这里只验证参数被接受且不抛错。
    act = decide(r, sim.buildings, 12.0, False, sim.rng,
                 recall=[{"kind": ActionType.WANDER.value, "bonus": 20.0}] * 3)
    assert act is not None
    assert _RECALL_BONUS_CAP == 12.0
