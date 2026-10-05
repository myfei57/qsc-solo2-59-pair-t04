"""按多项实测自动配组。

算法不再是"抄一个容量"，而是在四项实测（容量、内阻、开路电压、自放电率）上
同时满足组内允差。流程分三步，与缺口策略对应：

1. **硬限拦截**：单体绝对规格红线不过的，直接进 ``blocked``，任何策略都不装；
2. **合格组贪心**：剩余单体按容量排序，每次以容量最低者为锚，取一个四项极差
   全部不越限、且总体最紧的窗口成组。锚找不到足够合规邻居时，锚作为离群单体
   留待配池，等下一批；
3. **次优补位**（仅 ``fill_suboptimal``）：仍未成组的单体，取越限程度最轻的
   窗口凑出补位组，并逐只记下它放宽了哪几项、放宽到多少；``hold_next_batch``
   不做这一步，零头全部留池。

贪心取"最紧窗口"而不是"最先够格窗口"，是为了把最相似的单体留给彼此，减少
配到一半发现好单体被宽窗口浪费掉的返工。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .policy import GapPolicy
from .tolerance import GroupTolerance
from .units import UnitMeasurement


@dataclass(frozen=True)
class MatchedUnit:
    """一只单体在某个配组结果里的角色。"""

    unit_id: str
    waived: bool = False
    violated_limits: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "waived": self.waived,
            "violated_limits": list(self.violated_limits),
        }


@dataclass(frozen=True)
class MatchedGroup:
    """一次配组形成的组串候选。"""

    index: int
    units: tuple[MatchedUnit, ...]
    spreads_pct: dict[str, float]
    violated_limits: tuple[str, ...]
    suboptimal_fill: bool

    @property
    def conforming(self) -> bool:
        """是否为完全合规（无补位、无越限）的组。"""

        return not self.suboptimal_fill and not self.violated_limits

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "conforming": self.conforming,
            "suboptimal_fill": self.suboptimal_fill,
            "violated_limits": list(self.violated_limits),
            "spreads_pct": self.spreads_pct,
            "units": [unit.as_dict() for unit in self.units],
        }


@dataclass(frozen=True)
class GapReport:
    """本批配不齐部分的处置说明。"""

    policy: GapPolicy
    conforming_groups: int
    fill_groups: int
    waiting_units: tuple[str, ...]
    blocked: dict[str, str]
    short_by: int
    consequence: str

    def as_dict(self) -> dict:
        return {
            "policy": self.policy.value,
            "conforming_groups": self.conforming_groups,
            "fill_groups": self.fill_groups,
            "waiting_units": list(self.waiting_units),
            "waiting_count": len(self.waiting_units),
            "blocked": dict(self.blocked),
            "short_by": self.short_by,
            "consequence": self.consequence,
        }


@dataclass(frozen=True)
class MatchPlan:
    """一次配组的完整结果：组候选 + 待配池 + 硬限拦截 + 缺口说明。"""

    tolerance: GroupTolerance
    groups: tuple[MatchedGroup, ...]
    waiting_units: tuple[str, ...]
    blocked: dict[str, str]
    gap: GapReport

    def as_dict(self) -> dict:
        return {
            "tolerance": self.tolerance.as_dict(),
            "groups": [group.as_dict() for group in self.groups],
            "conforming_groups": self.gap.conforming_groups,
            "fill_groups": self.gap.fill_groups,
            "waiting_units": list(self.waiting_units),
            "blocked": dict(self.blocked),
            "gap": self.gap.as_dict(),
        }


@dataclass
class _Window:
    """候选窗口及其越限程度，用于在多个可行窗口间选最紧的一个。"""

    indices: list[int] = field(default_factory=list)
    violations: dict[str, float] = field(default_factory=dict)


def _nearest_neighbours(
    ordered: list[UnitMeasurement], anchor: int, usable: list[int], tolerance: GroupTolerance
) -> list[int]:
    """以 ``ordered[anchor]`` 为容量最低锚，选与锚综合最接近的 ``group_size-1`` 只。

    综合距离按四项指标各自的允差归一化后求和，量纲不同也能放在一起比；窗口必须
    含锚，避免"容量更高的批次相对极差更小"把锚挤成剩单。
    """

    size = tolerance.group_size
    peers = [index for index in usable if index > anchor][: max(size * 6, size)]
    anchor_measurement = ordered[anchor]
    limits = tolerance.spread_limits()

    def distance(index: int) -> float:
        candidate = ordered[index]
        total = 0.0
        for key, limit in limits.items():
            total += abs(candidate.value(key) - anchor_measurement.value(key)) / abs(anchor_measurement.value(key) or 1.0) * 100.0 / limit
        return total

    chosen = sorted(peers, key=lambda index: (distance(index), ordered[index].capacity_ah, index))[: size - 1]
    return [anchor] + sorted(chosen)


def _best_window(
    ordered: list[UnitMeasurement], anchor: int, usable: list[int], tolerance: GroupTolerance
) -> list[int]:
    """以锚为最低，找一个成员数齐全且四项极差都不越限的窗口；不足时返回空。"""

    if len(usable) < tolerance.group_size:
        return []
    window = _nearest_neighbours(ordered, anchor, usable, tolerance)
    if len(window) < tolerance.group_size:
        return []
    return [] if tolerance.spread_violations([ordered[index] for index in window]) else window


def _best_fill_window(
    ordered: list[UnitMeasurement], anchor: int, usable: list[int], tolerance: GroupTolerance
) -> _Window:
    """以锚为最低取一个窗口做次优补位（允许组内允差越限，硬限已在外层拦掉）。"""

    if len(usable) < tolerance.group_size:
        return _Window()
    window = _nearest_neighbours(ordered, anchor, usable, tolerance)
    if len(window) < tolerance.group_size:
        return _Window()
    violations = tolerance.spread_violations([ordered[index] for index in window])
    return _Window(window, violations)


def match_groups(
    measurements: list[UnitMeasurement], tolerance: GroupTolerance, policy: GapPolicy | str
) -> MatchPlan:
    """按容差与缺口策略，把一批实测单体配成组候选。"""

    selected = GapPolicy.resolve(policy)
    blocked: dict[str, str] = {}
    eligible: list[UnitMeasurement] = []
    for measurement in measurements:
        reasons = tolerance.hard_blocks(measurement)
        if reasons:
            blocked[measurement.unit_id] = ",".join(reasons)
        else:
            eligible.append(measurement)

    # 同一批里重复录入同一只单体没有配组意义，按标识去重并保留首次实测。
    deduped: dict[str, UnitMeasurement] = {}
    for measurement in eligible:
        deduped.setdefault(measurement.unit_id, measurement)
    ordered = sorted(deduped.values(), key=lambda item: (item.capacity_ah, item.unit_id))
    remaining = set(range(len(ordered)))

    matched: list[MatchedGroup] = []
    while len(remaining) >= tolerance.group_size:
        # 每轮都重新取当前容量最低锚。锚凑不齐合规邻居时立即停止：任何含更高锚
        # 的合规窗口都不包含本锚，跳过它只会把本锚逼进更宽的补位组——hold 模式
        # 必须把它原样留池等下一批，fill 模式也应让补位阶段显式承接。
        anchor = min(remaining)
        window = _best_window(ordered, anchor, sorted(remaining), tolerance)
        if not window:
            break
        spreads = {key: round(value, 4) for key, value in tolerance.spreads([ordered[i] for i in window]).items()}
        matched.append(
            MatchedGroup(
                index=len(matched),
                units=tuple(MatchedUnit(ordered[i].unit_id) for i in window),
                spreads_pct=spreads,
                violated_limits=(),
                suboptimal_fill=False,
            )
        )
        remaining.difference_update(window)

    fill_groups: list[MatchedGroup] = []
    if selected.allows_suboptimal_fill:
        while len(remaining) >= tolerance.group_size:
            # 补位阶段取当前容量最低锚强制成组：合格组阶段已证明没有含该锚的合规
            # 窗口，这里用与它最近的单体把组凑出来并如实标记越限项。
            anchor = min(remaining)
            window = _best_fill_window(ordered, anchor, sorted(remaining), tolerance)
            if not window.indices:
                break
            spreads = {
                key: round(value, 4) for key, value in tolerance.spreads([ordered[i] for i in window.indices]).items()
            }
            if not window.violations:
                units = [MatchedUnit(ordered[index].unit_id) for index in window.indices]
                matched.append(
                    MatchedGroup(
                        index=len(matched),
                        units=tuple(units),
                        spreads_pct=spreads,
                        violated_limits=(),
                        suboptimal_fill=False,
                    )
                )
            else:
                units = [
                    MatchedUnit(ordered[index].unit_id, waived=True, violated_limits=tuple(window.violations.keys()))
                    for index in window.indices
                ]
                fill_groups.append(
                    MatchedGroup(
                        index=len(matched) + len(fill_groups),
                        units=tuple(units),
                        spreads_pct=spreads,
                        violated_limits=tuple(window.violations.keys()),
                        suboptimal_fill=True,
                    )
                )
            remaining.difference_update(window.indices)

    groups = tuple(matched) + tuple(fill_groups)
    waiting = tuple(ordered[index].unit_id for index in sorted(remaining))
    # hold 模式：零头凑不出的整组都顺延下一批；fill 模式理论上已凑满，剩余仅为
    # 硬限外无法补位的散单，不再计入"等下一批的整组"。
    short_by = 0 if selected.allows_suboptimal_fill else len(waiting) // tolerance.group_size
    gap = GapReport(
        policy=selected,
        conforming_groups=len(matched),
        fill_groups=len(fill_groups),
        waiting_units=waiting,
        blocked=blocked,
        short_by=short_by,
        consequence=selected.consequence,
    )
    return MatchPlan(
        tolerance=tolerance,
        groups=groups,
        waiting_units=waiting,
        blocked=blocked,
        gap=gap,
    )
