"""元胞自动机「场」层 —— ABCA 里的「CA」。

三层解耦的最后一块拼图：

    world/grid.py   静态地形与占用（谁在哪、能不能走）
    world/fields.py 动态场（本模块）—— 每 tick 用固定的 扩散/衰减/源注入 规则
                    演化每一格上的一个标量，与智能体完全解耦
    agents/*        智能体只「读」场值，除投递一次性「源」外不改写场

CA 的一步（step）对每个扩散型通道统一执行：

    注入持久源 -> 注入一次性源 -> 四邻扩散 + 衰减 -> 钳制

光照（light）是特例：不扩散，由时钟统一驱动（昼夜 + 室内衰减）。

通道：
    smell  气味   源：厕所、餐馆      影响：心情（M3 内急链的社死传播）
    noise  噪声   源：工厂、广场、人群 影响：睡眠质量 / 心情
    crowd  人流   源：居民坐标        影响：寻路代价（蹭人流）
    light  光照   源：时钟           影响：作息（天黑了该回家）

设计红线：纯标准库、无第三方依赖；热数据、不落库；双缓冲避免顺序依赖。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

EPS = 1e-4  # 低于此值视为 0（用于稀疏活跃集剪枝）


@dataclass(frozen=True)
class ChannelSpec:
    """一个场通道的演化参数（无单位，均为 0..1 区间的比例）。"""

    name: str
    diffuse: float = 0.15   # 每步向四邻扩散的比例（>0.25 会数值不稳）
    decay: float = 0.02     # 每步自然衰减比例
    vmax: float = 1.0       # 浓度上限
    kind: str = "field"     # field（扩散型）| light（时钟驱动）


# 默认通道集合。smell / noise / light 三个够 M3 用；crowd 为 M4 预留。
DEFAULT_SPECS: tuple[ChannelSpec, ...] = (
    ChannelSpec("smell", diffuse=0.16, decay=0.05, vmax=1.0),
    ChannelSpec("noise", diffuse=0.22, decay=0.10, vmax=1.0),
    ChannelSpec("crowd", diffuse=0.20, decay=0.30, vmax=1.0),
    # 香味（2026-11-11）：餐馆/食堂/公园/酒吧/池塘 —— 「好闻」的独立通道。
    # 不能复用 smell：通道是非负标量，负值会被当 0 剪掉，香与臭必须两条通道。
    ChannelSpec("aroma", diffuse=0.20, decay=0.12, vmax=1.0),
    ChannelSpec("light", diffuse=0.0, decay=0.0, vmax=1.0, kind="light"),
)


def _light_ambient(hour: int) -> float:
    """昼夜环境光：06:00 起亮、18:00 后渐暗、夜间最低 0.05。"""
    if 6 <= hour < 18:
        return 0.6 + 0.4 * (1.0 - abs(hour - 12) / 6.0)
    if 18 <= hour < 21:
        return 0.6 - 0.55 * (hour - 18) / 3.0
    return 0.05


class FieldGrid:
    """与地形网格同尺寸的多通道标量场。

    用法::

        fg = FieldGrid(grid.w, grid.h)
        fg.sync_blocked(grid)                          # 墙体挡流
        fg.add_emitter("smell", x, y, r=3, amount=0.8) # 持久源
        fg.step(clock)                                 # 每 tick 演化一次
        v = fg.value("smell", x, y)
    """

    def __init__(
        self,
        width: int,
        height: int,
        specs: Iterable[ChannelSpec] = DEFAULT_SPECS,
        stride: int = 1,
    ):
        self.w = width
        self.h = height
        self.n = width * height
        self.stride = max(1, stride)
        self.specs: dict[str, ChannelSpec] = {s.name: s for s in specs}

        # 每通道双缓冲：_buf 为当前态，_scratch 为本步写入目标
        self._buf: dict[str, list[float]] = {k: [0.0] * self.n for k in self.specs}
        self._scratch: dict[str, list[float]] = {k: [0.0] * self.n for k in self.specs}

        self._blocked = bytearray(self.n)          # 1 = 不参与流动（墙体/水面）
        self._indoor = bytearray(self.n)           # 1 = 室内地板（光衰减用）

        self._emitters: list[tuple[str, int, int, int, float]] = []   # 持久源
        self._sources: list[tuple[str, int, int, float]] = []         # 一次性源

        # 稀疏剪枝：只演化「有值格子 + 邻居」。大多数格子是 0，不必全网格卷积。
        self._live: dict[str, set[int]] = {k: set() for k in self.specs}
        self._dirty: dict[str, set[int]] = {k: set() for k in self.specs}

        self._tick = 0
        self._ambient = 0.8
        self._light_key: float | None = None   # 光照缓存键（环境光变化才重算）

    # ------------------------------------------------------------------ 索引
    def idx(self, x: int, y: int) -> int:
        return y * self.w + x

    def in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x < self.w and 0 <= y < self.h

    # ------------------------------------------------------------------ 配置
    def sync_blocked(self, grid) -> None:
        """从世界网格同步「阻挡」与「室内」掩码。wall/water 挡流，floor 为室内。"""
        from .grid import Terrain  # 延迟导入，避免环形依赖

        for i in range(self.n):
            t = Terrain(grid.tiles[i])
            self._blocked[i] = 1 if t in (Terrain.WALL, Terrain.WATER) else 0
            self._indoor[i] = 1 if t == Terrain.FLOOR else 0

    # ------------------------------------------------------------------ 源
    def add_emitter(self, channel: str, x: int, y: int, radius: int, amount: float) -> None:
        """注册一个持久源：每步都在以 (x,y) 为心、radius 为半径内注入 amount。"""
        self._emitters.append((channel, x, y, radius, amount))

    def clear_emitters(self, channel: str | None = None) -> None:
        if channel is None:
            self._emitters.clear()
        else:
            self._emitters = [e for e in self._emitters if e[0] != channel]

    def add_source(self, channel: str, x: int, y: int, amount: float) -> None:
        """投递一次性源：下一步注入后即清空（居民/事件临时扰动）。"""
        self._sources.append((channel, x, y, amount))

    # ------------------------------------------------------------------ 读取
    def value(self, channel: str, x: int, y: int) -> float:
        if not self.in_bounds(x, y):
            return 0.0 if channel != "light" else self._ambient
        return self._buf[channel][self.idx(x, y)]

    @property
    def ambient(self) -> float:
        """当前环境光（0..1）。昼夜由 clock 驱动，外面用它做「天黑没黑」的软判断。"""
        return self._ambient

    def sample(self, channel: str, x: int, y: int) -> float:
        return self.value(channel, x, y)

    def peak(self, channel: str) -> float:
        buf = self._buf[channel]
        return max(buf) if buf else 0.0

    def export(self, channel: str) -> bytes:
        """导出为 0..255 位图（前端热力图渲染用）。"""
        vmax = self.specs[channel].vmax or 1.0
        buf = self._buf[channel]
        return bytes(min(255, int(v / vmax * 255.0)) for v in buf)

    def snapshot(self) -> dict:
        """峰值快照，供统计面板显示。"""
        return {k: round(self.peak(k), 3) for k in self.specs}

    # ------------------------------------------------------------------ CA 步
    def step(self, clock=None) -> None:
        """推进一个 CA 步。clock 可选：仅光照通道需要它。"""
        self._tick += 1

        # 1) 光照：时钟驱动，不扩散（每步都刷新）
        self._step_light(clock)

        # 2) 扩散型通道按 stride 演化（省算力）
        if self._tick % self.stride == 0:
            self._inject()
            for name, spec in self.specs.items():
                if spec.kind != "field":
                    continue
                self._diffuse_decay(name, spec)

    def _step_light(self, clock) -> None:
        if "light" not in self.specs:
            return
        if clock is not None:
            self._ambient = _light_ambient(clock.hour)
        # 环境光没变就不重算（一天只有少数几分钟会变）
        if self._light_key == self._ambient:
            return
        buf = self._buf["light"]
        amb = self._ambient
        for i in range(self.n):
            # 室内光衰减到 40%，室外为环境光
            buf[i] = amb * (0.4 if self._indoor[i] else 1.0)
        self._light_key = amb

    def _inject(self) -> None:
        """注入持久源与一次性源（就地叠加到当前缓冲）。"""
        for channel, x, y, radius, amount in self._emitters:
            self._stamp(channel, x, y, radius, amount)
        for channel, x, y, amount in self._sources:
            self._stamp(channel, x, y, 0, amount)
        self._sources.clear()

    def _stamp(self, channel: str, x: int, y: int, radius: int, amount: float) -> None:
        buf = self._buf[channel]
        live = self._live[channel]
        dirty = self._dirty[channel]
        vmax = self.specs[channel].vmax
        for yy in range(y - radius, y + radius + 1):
            for xx in range(x - radius, x + radius + 1):
                if not self.in_bounds(xx, yy):
                    continue
                i = self.idx(xx, yy)
                buf[i] = min(vmax, buf[i] + amount)
                live.add(i)
                dirty.add(i)

    def _diffuse_decay(self, name: str, spec: ChannelSpec) -> None:
        """稀疏四邻扩散 + 衰减 + 钳制；墙体挡流。双向缓冲避免顺序依赖。

        只遍历「活跃集」= 上一步有值的格子 ∪ 本步被注入的格子。存活下来的
        格子（值 > EPS）与它们的四邻组成下一轮的活跃集，其余格子保持 0。
        """
        src = self._buf[name]
        dst = self._scratch[name]
        w, h, blocked = self.w, self.h, self._blocked
        k = spec.diffuse
        keep = 1.0 - spec.decay
        vmax = spec.vmax

        live = self._live[name]
        seed = live | self._dirty[name]        # 本步需要更新（含被注入）的格子
        dirty = self._dirty[name]
        dirty.clear()

        next_live: set[int] = set()
        # dst 只在本步触及的格子上可能非零；先清零这些格子，其余保持 0
        for i in seed:
            dst[i] = 0.0

        for i in seed:
            if blocked[i]:
                continue
            x = i % w
            y = i // w
            center = src[i]
            s = 0.0
            cnt = 0
            if x > 0 and not blocked[i - 1]:
                s += src[i - 1]; cnt += 1
            if x < w - 1 and not blocked[i + 1]:
                s += src[i + 1]; cnt += 1
            if y > 0 and not blocked[i - w]:
                s += src[i - w]; cnt += 1
            if y < h - 1 and not blocked[i + w]:
                s += src[i + w]; cnt += 1
            avg = s / cnt if cnt else center
            v = (center + k * (avg - center)) * keep
            if v > vmax:
                v = vmax
            elif v < EPS:
                v = 0.0
            dst[i] = v
            if v > 0.0:
                next_live.add(i)
                # 邻居下步仍需更新（扩散波前）
                if x > 0:
                    next_live.add(i - 1)
                if x < w - 1:
                    next_live.add(i + 1)
                if y > 0:
                    next_live.add(i - w)
                if y < h - 1:
                    next_live.add(i + w)

        # 交换缓冲并更新活跃集
        self._buf[name], self._scratch[name] = dst, src
        live.clear()
        live.update(next_live)

    # ------------------------------------------------------------------ 其他
    def reset(self) -> None:
        for k in self.specs:
            for i in range(self.n):
                self._buf[k][i] = 0.0
            self._live[k].clear()
            self._dirty[k].clear()
        self._sources.clear()
        self._tick = 0
        self._light_key = None

    def live_cells(self, channel: str) -> int:
        """当前活跃格子数（性能诊断用）。"""
        return len(self._live[channel])
