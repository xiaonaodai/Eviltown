"""存储层（M6）：SQLite 落库 + 回放。

职责边界：
- 只负责"持久化"，不参与仿真逻辑，也不碰 HTTP。
- 行集格式对齐 DESIGN.md §4.2，接库时可直接平移。

子模块：
- db.py    连接与建表 schema
- repo.py  residents / relationships / events 三类行集写入
"""
from __future__ import annotations

from . import db, repo

__all__ = ["db", "repo"]
