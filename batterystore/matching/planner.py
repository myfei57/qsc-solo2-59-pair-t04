"""配组规划：按多项实测把单体分组，组内差异落在允差内。

算法是排序滑窗贪心：单体先按容量、内阻、电压排序，队首取一整组窗口核对
三项极差；窗口合格即成组，不合格就把造成超差的离群单体剔出留池，直到剩余
单体不足一组。排序让指标相近的单体优先同组，离群剔除保证少数异常单体不会
拖垮整批的成组率。

缺口处理固定为"宁可少配一组等下一批"：凑不齐或超差的单体留在池里，随下一批
到货重测后重新配组，本批少交付的组数记入 :attr:`MatchPlan.shortfall`。代价是
交付节奏后移一个批次；换来的是每组一致性不打折。另一条路——用次优单体补位
先把组凑出来——能保住本批交付节奏，但整组可用容量被最差那只单体钉死（木桶
效应），差异超标的组装出去仍要返工，正是本模块要根除的问题，所以不选。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from ..config import MatchingLimits
from .measurement import CellMeasurement

DEFER_TO_NEXT_BATCH = "defer_to_next_batch"

REASON_NOTES = {
    "capacity_spread": "与候选同组单体容量极差超差，留池等下一批",
    "resistance_spread": "与候选同组单体内阻极差超差，留池等下一批",
    "voltage_spread": "与候选同组单体电压极差超差，留池等下一批",
    "short_of_group": "数量不足一组，留池等下一批",
}

_METRIC_REASONS = {
    "capacity": "capacity_spread",
    "resistance": "resistance_spread",
    "voltage": "voltage_spread",
}


@dataclass(frozen=True)
class PlannedGroup:
    """一组配好的单体及其三项实测极差。"""

    sequence: int
    cells: tuple[CellMeasurement, ...]

    def spreads(self) -> dict:
        """返回组内三项指标的极差。"""

        return {
            "capacity_ah": round(_spread(self.cells, "capacity_ah"), 3),
            "resistance_mohm": round(_spread(self.cells, "resistance_mohm"), 3),
            "voltage_mv": round(_spread(self.cells, "voltage_v") * 1000.0, 3),
        }

    def as_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "cells": [measurement.cell_id for measurement in self.cells],
            "spreads": self.spreads(),
            "measurements": [measurement.as_dict() for measurement in self.cells],
        }


@dataclass(frozen=True)
class DeferredCell:
    """一只被留池的单体及其原因。"""

    measurement: CellMeasurement
    reason: str

    def as_dict(self) -> dict:
        return {
            "cell_id": self.measurement.cell_id,
            "reason": self.reason,
            "note": REASON_NOTES.get(self.reason, self.reason),
            "measurement": self.measurement.as_dict(),
        }


@dataclass(frozen=True)
class MatchPlan:
    """一次配组规划的结果。"""

    group_size: int
    requested_groups: int
    groups: tuple[PlannedGroup, ...]
    deferred: tuple[DeferredCell, ...]
    policy: str

    @property
    def shortfall(self) -> int:
        """相对需求还少几组。"""

        return max(0, self.requested_groups - len(self.groups))

    def policy_note(self) -> str:
        """说明缺口的处理方向与代价。"""

        if self.shortfall == 0:
            return "本批无缺口：需求组数全部按允差成组。"
        return (
            f"缺口 {self.shortfall} 组宁可少配、等下一批：{len(self.deferred)} 只单体留池，"
            f"随下一批到货重测后重新配组；代价是本批少交付 {self.shortfall} 组、"
            "交付节奏后移一个批次。不用次优单体补位——补位能保住本批交付节奏，"
            "但整组可用容量被最差单体钉死，装出去仍要返工。"
        )

    def as_dict(self) -> dict:
        return {
            "group_size": self.group_size,
            "requested_groups": self.requested_groups,
            "formed_groups": len(self.groups),
            "shortfall": self.shortfall,
            "policy": self.policy,
            "policy_note": self.policy_note(),
            "groups": [group.as_dict() for group in self.groups],
            "deferred": [cell.as_dict() for cell in self.deferred],
        }


def _spread(cells: tuple[CellMeasurement, ...] | list[CellMeasurement], attribute: str) -> float:
    values = [getattr(measurement, attribute) for measurement in cells]
    return max(values) - min(values)


def _window_violation(window: list[CellMeasurement], limits: MatchingLimits) -> str | None:
    """返回窗口内超差最严重的指标名；三项都合格返回 None。"""

    checks = (
        ("capacity", _spread(window, "capacity_ah"), limits.capacity_spread_ah),
        ("resistance", _spread(window, "resistance_mohm"), limits.resistance_spread_mohm),
        ("voltage", _spread(window, "voltage_v") * 1000.0, limits.voltage_spread_mv),
    )
    violations = [
        (metric, spread / limit if limit > 0 else float("inf"))
        for metric, spread, limit in checks
        if spread > limit
    ]
    if not violations:
        return None
    return max(violations, key=lambda item: item[1])[0]


def _outlier_index(window: list[CellMeasurement], metric: str) -> int:
    """在窗口里找出造成超差的离群单体下标。"""

    if metric == "capacity":
        # 窗口按容量升序，极差只由两端造成：比较掐头与去尾后哪种剩余极差更小。
        values = [measurement.capacity_ah for measurement in window]
        drop_head = values[-1] - values[1]
        drop_tail = values[-2] - values[0]
        return 0 if drop_head <= drop_tail else len(window) - 1
    if metric == "resistance":
        values = [measurement.resistance_mohm for measurement in window]
    else:
        values = [measurement.voltage_v for measurement in window]
    median = statistics.median(values)
    return max(range(len(window)), key=lambda index: abs(values[index] - median))


def plan_groups(
    measurements: list[CellMeasurement],
    limits: MatchingLimits,
    requested_groups: int | None = None,
) -> MatchPlan:
    """按允差把一批单体规划成若干组，返回成组与留池的完整结果。

    ``requested_groups`` 是本批交付目标；缺省按"理论可成组数"（单体数整除组
    大小）计，这样即使不显式给目标，超差剔池造成的缺口也会体现在
    :attr:`MatchPlan.shortfall` 里。
    """

    if not measurements:
        raise ValueError("没有实测数据可以配组")
    seen: set[str] = set()
    for measurement in measurements:
        if measurement.cell_id in seen:
            raise ValueError(f"实测数据里单体编号重复: {measurement.cell_id}")
        seen.add(measurement.cell_id)
    if requested_groups is not None and requested_groups < 0:
        raise ValueError("需求组数不能为负")

    pool = sorted(measurements, key=lambda item: (item.capacity_ah, item.resistance_mohm, item.voltage_v))
    groups: list[PlannedGroup] = []
    deferred: list[DeferredCell] = []
    while len(pool) >= limits.group_size:
        window = pool[: limits.group_size]
        violation = _window_violation(window, limits)
        if violation is None:
            groups.append(PlannedGroup(sequence=len(groups) + 1, cells=tuple(window)))
            del pool[: limits.group_size]
            continue
        drop = _outlier_index(window, violation)
        deferred.append(DeferredCell(measurement=pool[drop], reason=_METRIC_REASONS[violation]))
        del pool[drop]
    deferred.extend(DeferredCell(measurement=measurement, reason="short_of_group") for measurement in pool)

    requested = requested_groups if requested_groups is not None else len(measurements) // limits.group_size
    return MatchPlan(
        group_size=limits.group_size,
        requested_groups=requested,
        groups=tuple(groups),
        deferred=tuple(deferred),
        policy=DEFER_TO_NEXT_BATCH,
    )
