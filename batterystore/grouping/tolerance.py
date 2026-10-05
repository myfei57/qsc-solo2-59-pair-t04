"""配组容差。

容差分两层，不能混为一谈：

* **单体硬限**（绝对上下限）卡的是单只单体自己，落在区间外的单体即使补位也不
  允许装出去——这是安全/规格红线；
* **组内允差**（组内极差，按 ``(最大-最小)/最小`` 的百分数）卡的是同组一致性，
  决定哪几只可以挂在同一组串上。

允差按四项实测分别给出，任何一项越线该候选组都不算合格。组内极差用相对量而不是
绝对量，是因为容量、内阻、电压量纲不同，现场扩容规格时不用重新逐档调参。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import ConfigError
from .units import METRIC_KEYS, UnitMeasurement


@dataclass(frozen=True)
class GroupTolerance:
    """一组配组判据：单体硬限 + 四项组内允差（百分数）。"""

    group_size: int
    max_capacity_spread_pct: float
    max_resistance_spread_pct: float
    max_ocv_spread_pct: float
    max_self_discharge_spread_pct: float
    min_capacity_ah: float = 0.0
    max_resistance_mohm: float | None = None
    max_self_discharge_pct: float | None = None

    def __post_init__(self) -> None:
        if self.group_size < 1:
            raise ConfigError("每组单体数必须为正", component="grouping")
        spreads = self.spread_limits()
        for key in METRIC_KEYS:
            if spreads[key] <= 0:
                raise ConfigError(f"组内允差 {key} 必须为正", component="grouping")
        if self.min_capacity_ah < 0:
            raise ConfigError("容量下限不能为负", component="grouping")

    def spread_limits(self) -> dict[str, float]:
        """返回四项指标的组内允差上限（百分数）。"""

        return {
            "capacity_ah": self.max_capacity_spread_pct,
            "resistance_mohm": self.max_resistance_spread_pct,
            "ocv_v": self.max_ocv_spread_pct,
            "self_discharge_pct": self.max_self_discharge_spread_pct,
        }

    def hard_blocks(self, measurement: UnitMeasurement) -> list[str]:
        """返回该单体自己触发的硬限项；为空表示可以参与配组。"""

        blocked: list[str] = []
        if measurement.capacity_ah < self.min_capacity_ah:
            blocked.append("capacity_below_min")
        if self.max_resistance_mohm is not None and measurement.resistance_mohm > self.max_resistance_mohm:
            blocked.append("resistance_above_max")
        if (
            self.max_self_discharge_pct is not None
            and measurement.self_discharge_pct > self.max_self_discharge_pct
        ):
            blocked.append("self_discharge_above_max")
        return blocked

    def spreads(self, measurements: list[UnitMeasurement]) -> dict[str, float]:
        """计算一组单体在四项指标上的实际组内极差（百分数）。"""

        result: dict[str, float] = {}
        for key in METRIC_KEYS:
            values = [item.value(key) for item in measurements]
            lower = min(values)
            result[key] = 0.0 if lower <= 0 else (max(values) - lower) / lower * 100.0
        return result

    def spread_violations(self, measurements: list[UnitMeasurement]) -> dict[str, float]:
        """返回超出允差的指标及其实测极差（百分数）；空字典表示完全合规。"""

        if len(measurements) < 2:
            return {}
        return {
            key: round(spread, 4)
            for key, spread in self.spreads(measurements).items()
            if spread > self.spread_limits()[key] + 1e-9
        }

    def as_dict(self) -> dict:
        return {
            "group_size": self.group_size,
            "spread_limits_pct": {key: round(value, 4) for key, value in self.spread_limits().items()},
            "min_capacity_ah": self.min_capacity_ah,
            "max_resistance_mohm": self.max_resistance_mohm,
            "max_self_discharge_pct": self.max_self_discharge_pct,
        }
