"""网格地形与占用表。

网格管物理：地形是否可走、每格被谁占用。热数据，不落库。
"""
from __future__ import annotations

import random
from enum import IntEnum


class Terrain(IntEnum):
    GRASS = 0
    ROAD = 1
    WATER = 2
    TREE = 3
    SAND = 4
    FLOOR = 5   # 建筑室内地板
    WALL = 6


WALKABLE = {Terrain.GRASS, Terrain.ROAD, Terrain.SAND, Terrain.FLOOR}


class Grid:
    def __init__(self, width: int, height: int, seed: int = 42):
        self.w = width
        self.h = height
        self.rng = random.Random(seed)
        self.tiles = bytearray(width * height)          # Terrain 值
        self.occupant: list[int | None] = [None] * (width * height)  # resident id
        self._gen_terrain()

    # --- 索引 ---
    def idx(self, x: int, y: int) -> int:
        return y * self.w + x

    def in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x < self.w and 0 <= y < self.h

    def terrain(self, x: int, y: int) -> Terrain:
        return Terrain(self.tiles[self.idx(x, y)])

    def set_terrain(self, x: int, y: int, t: Terrain) -> None:
        self.tiles[self.idx(x, y)] = int(t)

    def walkable(self, x: int, y: int) -> bool:
        if not self.in_bounds(x, y):
            return False
        t = Terrain(self.tiles[self.idx(x, y)])
        if t not in WALKABLE:
            return False
        # 室内地板与路面允许多人重叠（避免阻塞），但草地/沙地也允许
        return True

    # --- 占用 ---
    def occupant_at(self, x: int, y: int) -> int | None:
        return self.occupant[self.idx(x, y)]

    def move_occupant(self, rid: int, fx: int, fy: int, tx: int, ty: int) -> None:
        if self.in_bounds(fx, fy) and self.occupant[self.idx(fx, fy)] == rid:
            self.occupant[self.idx(fx, fy)] = None
        self.occupant[self.idx(tx, ty)] = rid

    def place(self, rid: int, x: int, y: int) -> None:
        self.occupant[self.idx(x, y)] = rid

    # --- 初始地形 ---
    def _gen_terrain(self) -> None:
        rng = self.rng
        for y in range(self.h):
            for x in range(self.w):
                r = rng.random()
                if r < 0.06:
                    t = Terrain.TREE
                elif r < 0.09:
                    t = Terrain.SAND
                else:
                    t = Terrain.GRASS
                self.set_terrain(x, y, t)

    def export_tiles(self) -> bytes:
        """导出地形位图（前端渲染用）。"""
        return bytes(self.tiles)
