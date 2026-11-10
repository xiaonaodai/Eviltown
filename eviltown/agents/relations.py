"""关系图层：居民之间的社会关系网（内存版）。

「图管社会」——节点是居民，边是带权关系：
  - trust     信任   -100..100（可为负，敌人）
  - intimacy  亲密度    0..100（决定能解锁多亲密的交互）
  - kind      关系类型（陌生人/认识/朋友/挚友/恋人/配偶/对手/仇敌）
  - encounters 累计相遇次数
  - last_day  最近一次互动的仿真日（用于「久未联系」衰减）

本轮为**内存实现**，接口刻意对齐 DESIGN.md §4.2 的 `relationships` 表，
M6 接 SQLite 时可直接平移落库（`snapshot()` 即行集）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RelKind(str, Enum):
    STRANGER = "陌生人"
    ACQUAINTANCE = "认识"
    FRIEND = "朋友"
    BEST_FRIEND = "挚友"
    LOVER = "恋人"
    SPOUSE = "配偶"
    RIVAL = "对手"
    ENEMY = "仇敌"


@dataclass
class Relationship:
    """一条无向边（a < b）。关系是相互的，但留存双向可扩展的非对称字段。"""
    a: int
    b: int
    trust: float = 0.0
    intimacy: float = 0.0
    kind: RelKind = RelKind.STRANGER
    encounters: int = 0
    # 分类型相遇计数 {交互名: 次数}：用于「同一类交互各数各的」判定
    # ——打招呼 3 次 = 认识、聊天 3 次 = 认识，互不叠加。冲突类不计入（见 sim._socialize）。
    kind_counts: dict[str, int] = field(default_factory=dict)
    last_day: int = 0
    married: bool = False

    def pair(self) -> tuple[int, int]:
        return (self.a, self.b)

    def other(self, rid: int) -> int:
        return self.b if rid == self.a else self.a

    def max_kind_count(self) -> int:
        """同一类交互的最高重复次数（无记录则 0）。"""
        return max(self.kind_counts.values(), default=0)

    def snapshot(self) -> dict:
        return {
            "a": self.a, "b": self.b,
            "trust": round(self.trust, 1),
            "intimacy": round(self.intimacy, 1),
            "kind": self.kind.value,
            "encounters": self.encounters,
            "married": self.married,
        }


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def classify(trust: float, intimacy: float, married: bool = False,
             max_kind_count: int = 0) -> RelKind:
    """按强度把数值关系映射到可读的类型。数值是主体，标签只是投影。

    `max_kind_count` 为「同一类交互的最高重复次数」——达到 3 次即认定为
    「认识」（用户规格：打招呼 3 次 / 聊天 3 次，各数各的、互不叠加）。
    """
    if married:
        return RelKind.SPOUSE
    if trust <= -40:
        return RelKind.ENEMY
    if trust <= -15:
        return RelKind.RIVAL
    if intimacy >= 80 and trust >= 40:
        return RelKind.LOVER
    if intimacy >= 60 and trust >= 30:
        return RelKind.BEST_FRIEND
    if intimacy >= 25 or trust >= 20:
        return RelKind.FRIEND
    if intimacy >= 5 or trust >= 5 or max_kind_count >= 3:
        return RelKind.ACQUAINTANCE
    return RelKind.STRANGER


class RelationGraph:
    """社会的邻接表。用 dict-of-edge，键为有序 (a, b) 元组。

    对 50 人的小镇，边数上限 C(50,2)=1225，内存无压力，故不做稀疏优化。
    """

    def __init__(self) -> None:
        self._edges: dict[tuple[int, int], Relationship] = {}

    # ---------------------------------------------------------------- 读写
    @staticmethod
    def _key(a: int, b: int) -> tuple[int, int]:
        return (a, b) if a < b else (b, a)

    def get(self, a: int, b: int) -> Relationship:
        """取边，不存在则惰性创建（初次相遇即建边）。"""
        if a == b:
            raise ValueError("关系边两端不能是同一人")
        k = self._key(a, b)
        rel = self._edges.get(k)
        if rel is None:
            rel = Relationship(a=k[0], b=k[1])
            self._edges[k] = rel
        return rel

    def adjust(
        self, a: int, b: int, *, trust: float = 0.0, intimacy: float = 0.0,
        day: int = 0, encounter: bool = True, kind: str | None = None,
    ) -> Relationship:
        """施加一次关系变化，并自动重算关系类型。返回更新后的边。

        `kind` 为本次交互名（如「打招呼」/「聊天」）：传入则该类计数 +1，
        用于「同一类交互各数各的」判定；冲突类由调用方传 None（不计入）。
        """
        rel = self.get(a, b)
        rel.trust = _clamp(rel.trust + trust, -100.0, 100.0)
        rel.intimacy = _clamp(rel.intimacy + intimacy, 0.0, 100.0)
        if encounter:
            rel.encounters += 1
        if kind:
            rel.kind_counts[kind] = rel.kind_counts.get(kind, 0) + 1
        rel.last_day = day
        rel.kind = classify(rel.trust, rel.intimacy, rel.married,
                            rel.max_kind_count())
        return rel

    def marry(self, a: int, b: int) -> Relationship:
        rel = self.get(a, b)
        rel.married = True
        rel.intimacy = max(rel.intimacy, 85.0)
        rel.trust = max(rel.trust, 60.0)
        rel.kind = classify(rel.trust, rel.intimacy, rel.married,
                            rel.max_kind_count())
        return rel

    # ---------------------------------------------------------------- 查询
    def neighbors(self, rid: int) -> list[Relationship]:
        return [e for e in self._edges.values() if rid in (e.a, e.b)]

    def friends_of(self, rid: int) -> list[int]:
        """把「朋友及以上」的邻居返回（快脑的社会加成用）。"""
        good = {RelKind.FRIEND, RelKind.BEST_FRIEND, RelKind.LOVER, RelKind.SPOUSE}
        return [e.other(rid) for e in self._edges.values()
                if rid in (e.a, e.b) and e.kind in good]

    def edge_count(self) -> int:
        return len(self._edges)

    def stats(self) -> dict:
        by_kind: dict[str, int] = {}
        for e in self._edges.values():
            by_kind[e.kind.value] = by_kind.get(e.kind.value, 0) + 1
        return {
            "edges": len(self._edges),
            "by_kind": by_kind,
        }

    def snapshot(self, top: int = 30) -> list[dict]:
        """按亲密度降序导出若干强边（供 UI 画关系线）。"""
        ordered = sorted(self._edges.values(),
                         key=lambda e: (e.intimacy, abs(e.trust)), reverse=True)
        return [e.snapshot() for e in ordered[:top]]
