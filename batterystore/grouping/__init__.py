"""产线配组：按多项实测把单体自动配成组串、登记组串与簇、保留重配履历。"""

from __future__ import annotations

from .ledger import GroupingLedger
from .match import MatchedGroup, MatchedUnit, MatchPlan, match_groups
from .policy import GapPolicy
from .tolerance import GroupTolerance
from .units import METRIC_KEYS, UnitMeasurement


def default_tolerance(group_size: int = 13) -> GroupTolerance:
    """磷酸铁锂单串常见分选工艺窗口，作为未显式给参时的默认判据。"""

    return GroupTolerance(
        group_size=group_size,
        max_capacity_spread_pct=1.5,
        max_resistance_spread_pct=5.0,
        max_ocv_spread_pct=0.5,
        max_self_discharge_spread_pct=20.0,
        min_capacity_ah=100.0,
        max_resistance_mohm=0.8,
        max_self_discharge_pct=5.0,
    )


__all__ = [
    "METRIC_KEYS",
    "GapPolicy",
    "GroupTolerance",
    "GroupingLedger",
    "MatchedGroup",
    "MatchedUnit",
    "MatchPlan",
    "UnitMeasurement",
    "default_tolerance",
    "match_groups",
]
