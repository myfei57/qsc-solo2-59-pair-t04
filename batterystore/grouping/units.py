"""待配单体与其实测结果。

配组面对的是分选台上的裸单体，不是在运簇里的 :class:`batterystore.rack.cells.Cell`，
因此这里单独建模。人工抄容量之所以组内差异大，根因是只看一个维度；自动配组必须
同时吃进容量、内阻、开路电压、自放电率四项实测，任何一项越限都不算合格组。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 参与配组的四项实测指标，顺序即配组主轴顺序（容量为主键）。
METRIC_KEYS = ("capacity_ah", "resistance_mohm", "ocv_v", "self_discharge_pct")


@dataclass(frozen=True)
class UnitMeasurement:
    """一只单体的一次分选实测录入。"""

    unit_id: str
    capacity_ah: float
    resistance_mohm: float
    ocv_v: float
    self_discharge_pct: float
    batch_id: str = ""
    measured_ts: float = 0.0

    def value(self, key: str) -> float:
        """按指标键取实测值。"""

        return float(getattr(self, key))

    def as_dict(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "capacity_ah": round(self.capacity_ah, 4),
            "resistance_mohm": round(self.resistance_mohm, 4),
            "ocv_v": round(self.ocv_v, 5),
            "self_discharge_pct": round(self.self_discharge_pct, 4),
            "batch_id": self.batch_id,
            "measured_ts": round(self.measured_ts, 3),
        }


@dataclass
class Unit:
    """台账内一只可配单体，带当前装配状态。"""

    measurement: UnitMeasurement
    status: str = "pool"
    batch_id: str = ""

    @property
    def unit_id(self) -> str:
        return self.measurement.unit_id

    def value(self, key: str) -> float:
        return self.measurement.value(key)

    def as_dict(self) -> dict:
        payload = self.measurement.as_dict()
        payload["status"] = self.status
        return payload
