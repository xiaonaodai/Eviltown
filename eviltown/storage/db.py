"""SQLite 连接与建表。

落库策略（用户确认）：**每天快照**——跨日时把当日 residents / relationships
写一份带 day 主键的行集，events 逐日累积。这样既可回放，又不会膨胀。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS residents_daily (
    day INTEGER NOT NULL,
    rid INTEGER NOT NULL,
    name TEXT, sex TEXT, age INTEGER, job TEXT,
    x INTEGER, y INTEGER, action TEXT, mood REAL,
    satiety REAL, hunger REAL, bladder REAL, energy REAL, social REAL,
    openness REAL, conscientiousness REAL, extraversion REAL,
    agreeableness REAL, neuroticism REAL,
    PRIMARY KEY (day, rid)
);

CREATE TABLE IF NOT EXISTS relationships_daily (
    day INTEGER NOT NULL,
    a INTEGER NOT NULL,
    b INTEGER NOT NULL,
    trust REAL, intimacy REAL, kind TEXT,
    encounters INTEGER, married INTEGER,
    PRIMARY KEY (day, a, b)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day INTEGER, tick INTEGER,
    kind TEXT, category TEXT,
    actor INTEGER, target INTEGER, fx TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_day ON events(day);

-- 统计日报：一行一天。data 为 JSON —— 9 个桶（白天 8 个 2 小时桶 + 整夜 1 桶），
-- 每桶含各动作的独立人数与人次。统计日按 06:00 起算（见 sim.py:_report_day_of）。
CREATE TABLE IF NOT EXISTS daily_stats (
    day INTEGER PRIMARY KEY,
    data TEXT NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开（并初始化）数据库。父目录不存在会自动创建。"""
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL;")
    con.executescript(SCHEMA)
    _migrate(con)
    con.commit()
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """轻量幂等迁移：给已存在的旧库补上后来新增的列。

    ⚠️ 建表用的是 `CREATE TABLE IF NOT EXISTS`——**对已存在的老库不会加列**。
    2026-09-30 新增 `satiety`（对外饱食度 = 100 − hunger），若不迁移，
    `repo.save_residents` 跨日落库时会抛 `no such column: satiety`。
    这里逐列检查 + ALTER，重复调用安全（已有列则跳过）。
    """
    have = {row[1] for row in con.execute("PRAGMA table_info(residents_daily)")}
    for col in ("satiety",):        # 后续新增列往这里追加即可
        if col not in have:
            con.execute(f"ALTER TABLE residents_daily ADD COLUMN {col} REAL")
