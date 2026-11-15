"""小镇长期记忆：把历史写成「能被检索到的一句话」，决策 / 日记前召回（轻量 RAG）。

为什么是这么一层：
- **记忆源**是 `memory_events` 表（storage/db.py），只收「值得记住」的摘要
  （在哪干了什么、跟谁），不收每 tick 的原始流水；
- **召回**只做两件事：给 `brain.decide()` 的候选行为加一点软权重（≤ `rag.weight`
  分）、给日记 prompt 塞 1~2 条「你还记得…」。**绝不删候选、绝不改阈值** ——
  规则层始终有最终解释权，记忆只是「偏好」，不能把作息与生计顶翻；
- **检索**在 storage/vector_store.py：sqlite-vec → 纯 Python 余弦 → 关键词，
  三级降级，任何一级挂掉都往下掉，最差也有词面结果。

红线（AGENTS.md）：**LLM 不进主循环**。`add()` 只做一次 INSERT；embedding 由
调用方（后台线程）算好传进来，默认 `rag.embed=false` 就是纯关键词，零网络。

时间衰减：`score * 0.5 ** (age / half_life_days)` —— 一周前的记忆权重减半，
这样「昨天在酒吧被搭话」比「三十天前在酒吧」更容易影响今天的选择。
"""
from __future__ import annotations

import math
from pathlib import Path

from ..storage import db as storage_db
from ..storage.vector_store import VectorStore

# 没有可召回内容时的空列表（调用方直接 for 就行，不用判 None）
_EMPTY: list[dict] = []


def query_from_state(action: str = "", hour: float = 0.0,
                     place: str | None = None, extra: str = "") -> str:
    """把「此刻的自己」拼成一句检索串。供 sim 在决策前调用（纯字符串，无副作用）。"""
    parts = []
    if action:
        parts.append(str(action))
    if place:
        parts.append(str(place))
    parts.append(f"{int(hour) % 24}点")
    if extra:
        parts.append(str(extra))
    return " ".join(parts)


class TownHistory:
    """`memory_events` 的读写在应用层的样子：写一句话 / 按当下状态召回。"""

    def __init__(self, cfg=None, *, con=None, db_path: str | None = None,
                 storage_cfg=None, embed_fn=None) -> None:
        self.cfg = cfg
        self.enabled = bool(getattr(cfg, "enabled", False)) if cfg is not None else False
        self.recall_k = int(getattr(cfg, "recall_k", 2) or 2)
        self.weight = float(getattr(cfg, "weight", 6.0) or 0.0)
        self.half_life = float(getattr(cfg, "half_life_days", 7.0) or 7.0)
        self.diary_on = bool(getattr(cfg, "diary", True))
        self.embed_fn = embed_fn

        self._con = con
        self._own_con = False
        path = db_path or str(getattr(storage_cfg, "db_path", "")
                              or "data/eviltown.db")
        if self._con is None and self.enabled:
            # 只在自己建连接时碰文件系统；测试里传 :memory: 的连接即可。
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._con = storage_db.connect(path)
            self._own_con = True
        self.store = VectorStore(self._con, use_vec=bool(self.embed_fn),
                                 dim=None) if self._con is not None else None

    # ------------------------------------------------------------ 写入
    def add(self, *, day: int, tick: int, kind: str, text: str,
            actor: int | None = None, target: int | None = None,
            place: str | None = None, weight: float = 1.0) -> int:
        """记一句。关闭 / 无连接 / 空文本 → 返回 -1，**绝不抛异常**给主循环。"""
        if not self.enabled or self.store is None:
            return -1
        try:
            emb = None
            if self.embed_fn is not None:
                try:
                    emb = self.embed_fn(text)
                except Exception:  # noqa: BLE001  embedding 挂了就是没向量，不致命
                    emb = None
            return self.store.add(day=day, tick=tick, kind=kind, text=text,
                                  actor=actor, target=target, place=place,
                                  weight=weight, embedding=emb)
        except Exception:  # noqa: BLE001  记忆写失败不该打断仿真
            return -1

    def flush(self) -> None:
        """提交当前事务（日终 / 测试里调用）。没有连接时静默返回。"""
        if self._con is not None:
            try:
                self._con.commit()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        """只关自己 opened 的连接；外部灌进来的连接由外部管。"""
        if self._own_con and self._con is not None:
            try:
                self._con.close()
            except Exception:  # noqa: BLE001
                pass
            self._con = None

    # ------------------------------------------------------------ 召回
    def _decay(self, day: int, now_day: int | None) -> float:
        if now_day is None or self.half_life <= 0:
            return 1.0
        age = max(0, int(now_day) - int(day))
        return float(0.5 ** (age / self.half_life))

    def search(self, query: str, *, now_day: int | None = None, k: int | None = None,
               actor: int | None = None) -> list[dict]:
        """按相关度召回，附 `decayed` / `bonus` 两个派生字段。

        `bonus` 就是 `decide()` 直接加在候选效用上的分数（已按 Weight 与时间
        衰减折扣，并封顶在 `cfg.weight`），所以调用方不需要再算。
        """
        if not self.enabled or self.store is None or not (query or "").strip():
            return list(_EMPTY)
        kk = int(k or self.recall_k)
        try:
            qv = None
            if self.embed_fn is not None:
                try:
                    qv = self.embed_fn(query)
                except Exception:  # noqa: BLE001
                    qv = None
            hits = self.store.search(query, query_vec=qv, k=max(kk * 3, kk),
                                     actor=actor)
        except Exception:  # noqa: BLE001
            return list(_EMPTY)
        out = []
        for h in hits:
            decay = self._decay(h.day, now_day)
            val = max(0.0, min(1.0, h.score)) * decay * min(2.0, max(0.5, h.weight))
            out.append({
                **h.as_dict(),
                "decayed": round(decay, 4),
                "bonus": round(min(self.weight, val * self.weight), 3),
            })
        out.sort(key=lambda d: d["bonus"], reverse=True)
        return out[:kk]

    def recall(self, query: str, *, now_day: int | None = None,
               r=None, k: int | None = None) -> list[dict]:
        """决策用的召回：只看「这个人」和「全城都该记得」的记忆（actor 过滤）。"""
        actor = getattr(r, "rid", None) if r is not None else None
        return self.search(query, now_day=now_day, k=k, actor=actor)

    def remember_lines(self, query: str, *, now_day: int | None = None,
                       r=None, limit: int = 2) -> list[str]:
        """日记 prompt 用：1~2 行「你还记得…」。关闭时不返回任何东西。"""
        if not self.enabled or not self.diary_on:
            return []
        hits = self.recall(query, now_day=now_day, r=r, k=limit)
        return [f"你还记得：{h['text']}" for h in hits if h.get("text")]

    # ------------------------------------------------------------ 诊断
    def stats(self) -> dict:
        if self.store is None:
            return {"enabled": self.enabled, "backend": "off", "rows": 0}
        return {"enabled": self.enabled, **self.store.stats(),
                "half_life_days": self.half_life, "weight": self.weight}


def event_to_memory(ev: dict, *, day: int, tick: int) -> tuple[str, str] | None:
    """把一条仿真事件压成「一句能被检索的话」。

    返回 `(kind, text)`；不值得记住的返回 None（比如纯数值波动）。
    `kind` 用行为类型值（eat / bar / fish …），这样 `decide()` 能按
    `ActionType.value` 直接对上，不需要额外的映射表。
    """
    kind = str(ev.get("kind") or ev.get("category") or "").strip()
    text = str(ev.get("text") or ev.get("msg") or "").strip()
    if not kind or not text:
        return None
    # 只在句子里补上时间与地点，方便日后关键词命中（「上周在酒吧」）。
    place = ev.get("place")
    if place and str(place) not in text:
        text = f"{text}（{place}）"
    return kind, text
