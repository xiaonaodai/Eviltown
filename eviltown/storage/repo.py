"""行集写入：把 snapshot 数据写进 SQLite。

所有函数都接收「可迭代的行集」，不依赖具体类，方便测试与复用。
"""
from __future__ import annotations

import sqlite3

_RESIDENT_SQL = """INSERT OR REPLACE INTO residents_daily
(day, rid, name, sex, age, job, x, y, action, mood,
 satiety, hunger, bladder, energy, social,
 openness, conscientiousness, extraversion, agreeableness, neuroticism)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""

_REL_SQL = """INSERT OR REPLACE INTO relationships_daily
(day, a, b, trust, intimacy, kind, encounters, married)
VALUES (?,?,?,?,?,?,?,?)"""

_EVENT_SQL = """INSERT INTO events (day, tick, kind, category, actor, target, fx)
VALUES (?,?,?,?,?,?,?)"""


def save_residents(con: sqlite3.Connection, day: int, residents) -> int:
    """写当日居民快照。residents 为带 .snapshot() 的对象列表。"""
    rows = []
    for r in residents:
        s = r.snapshot()
        need = s.get("need") or {}
        oc = s.get("ocean") or {}
        rows.append((
            day, s.get("id"), s.get("name"), s.get("sex"), s.get("age"),
            s.get("job"), s.get("x"), s.get("y"), s.get("action"), s.get("mood"),
            need.get("satiety"), need.get("hunger"),
            need.get("bladder"), need.get("energy"), need.get("social"),
            oc.get("openness"), oc.get("conscientiousness"), oc.get("extraversion"),
            oc.get("agreeableness"), oc.get("neuroticism"),
        ))
    con.executemany(_RESIDENT_SQL, rows)
    return len(rows)


def save_relations(con: sqlite3.Connection, day: int, edges) -> int:
    """写当日关系快照。edges 为 relations.snapshot() 产出的 dict 列表。"""
    rows = [(
        day, e["a"], e["b"], e["trust"], e["intimacy"], e["kind"],
        e["encounters"], int(e.get("married", False)),
    ) for e in edges]
    con.executemany(_REL_SQL, rows)
    return len(rows)


def save_events(con: sqlite3.Connection, tick: int, events) -> int:
    """写事件流。events 为 sim.events 里的 dict。"""
    rows = [(
        e.get("day"), int(e.get("tick", tick)), e.get("kind"), e.get("category"),
        e.get("actor"), e.get("target"), e.get("fx"),
    ) for e in events]
    con.executemany(_EVENT_SQL, rows)
    return len(rows)


def save_daily_stats(con: sqlite3.Connection, day: int, data: dict) -> int:
    """写某天的统计日报（一行 JSON）。同一天重复写用 REPLACE 覆盖。"""
    import json

    con.execute(
        "INSERT OR REPLACE INTO daily_stats (day, data) VALUES (?, ?)",
        (int(day), json.dumps(data, ensure_ascii=False)),
    )
    return 1


def load_daily_stats(con: sqlite3.Connection, day: int) -> dict | None:
    """按天读日报；这天没落过库就返回 None。"""
    import json

    row = con.execute(
        "SELECT data FROM daily_stats WHERE day = ?", (int(day),)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None
