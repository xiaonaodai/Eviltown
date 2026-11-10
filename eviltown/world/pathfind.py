"""A* 寻路（网格四/八邻接）。

热路径：每 tick 大量调用，尽量轻。缓存由调用方负责。
"""
from __future__ import annotations

import heapq
import random
from collections.abc import Callable

SQRT2 = 1.41421356


def a_star(
    start: tuple[int, int],
    goal: tuple[int, int],
    walkable: Callable[[int, int], bool],
    diagonal: bool = True,
    max_nodes: int = 60000,   # ≥ 240×160 全图节点数；避免大图上误判「不可达」
    rng: random.Random | None = None,
) -> list[tuple[int, int]]:
    """返回从 start 到 goal 的路径（含终点，不含起点）；不可达返回 []。

    rng：传入时给同代价节点加极小随机噪声（< 最小步长代价 1.0），只在
    「并列最优」的路径之间改变取舍，不破坏最优性——用于打破「全城都走
    同一条最短路径」的确定性晶体化，让人流自然错开。
    """
    if start == goal:
        return []
    if not walkable(*goal):
        return []

    if diagonal:
        steps = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                 (1, 1, SQRT2), (1, -1, SQRT2), (-1, 1, SQRT2), (-1, -1, SQRT2)]
    else:
        steps = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0)]

    def h(a: tuple[int, int], b: tuple[int, int]) -> float:
        dx, dy = abs(a[0] - b[0]), abs(a[1] - b[1])
        if diagonal:
            return (dx + dy) + (SQRT2 - 2) * min(dx, dy)
        return dx + dy

    jitter = (lambda: rng.random() * 1e-3) if rng is not None else (lambda: 0.0)
    open_heap: list[tuple[float, tuple[int, int]]] = [(h(start, goal) + jitter(), start)]
    came: dict[tuple[int, int], tuple[int, int]] = {}
    g: dict[tuple[int, int], float] = {start: 0.0}
    visited = 0

    while open_heap:
        _, cur = heapq.heappop(open_heap)
        if cur == goal:
            path = []
            while cur != start:
                path.append(cur)
                cur = came[cur]
            path.reverse()
            return path
        visited += 1
        if visited > max_nodes:
            break
        cx, cy = cur
        for dx, dy, cost in steps:
            nx, ny = cx + dx, cy + dy
            if not walkable(nx, ny):
                continue
            ng = g[cur] + cost
            nb = (nx, ny)
            if ng < g.get(nb, 1e18):
                g[nb] = ng
                came[nb] = cur
                heapq.heappush(open_heap, (ng + h(nb, goal) + jitter(), nb))
    return []
