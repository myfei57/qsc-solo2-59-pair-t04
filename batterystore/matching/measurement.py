"""单体实测数据。

配组只信实测：容量决定整组可放电量，内阻决定发热与压降的一致性，开路电压
反映自放电差异。三项分别来自充放电测试、内阻仪与静置采集，任何一项缺失或
取值越出物理合理范围都不允许进场——过去靠人工抄标称值配组，组内差异大、
装出去返工，根源就是输入数据不可信。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class CellMeasurement:
    """一只单体的一组实测结果（容量、内阻、开路电压）。"""

    cell_id: str
    capacity_ah: float
    resistance_mohm: float
    voltage_v: float

    def __post_init__(self) -> None:
        if not self.cell_id:
            raise ValueError("单体编号不能为空")
        if self.capacity_ah <= 0:
            raise ValueError("实测容量必须为正")
        if self.resistance_mohm <= 0:
            raise ValueError("实测内阻必须为正")
        if self.voltage_v <= 0:
            raise ValueError("实测电压必须为正")

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "CellMeasurement":
        """从映射解析一只单体的实测记录，缺字段或取值非法即拒绝。"""

        if not isinstance(mapping, Mapping):
            raise ValueError("实测记录必须是映射")
        try:
            return cls(
                cell_id=str(mapping["cell_id"]),
                capacity_ah=float(mapping["capacity_ah"]),
                resistance_mohm=float(mapping["resistance_mohm"]),
                voltage_v=float(mapping["voltage_v"]),
            )
        except KeyError as error:
            raise ValueError(f"实测记录缺少字段: {error}") from error
        except (TypeError, ValueError) as error:
            raise ValueError(f"实测记录字段非法: {error}") from error

    def as_dict(self) -> dict:
        return {
            "cell_id": self.cell_id,
            "capacity_ah": round(self.capacity_ah, 3),
            "resistance_mohm": round(self.resistance_mohm, 3),
            "voltage_v": round(self.voltage_v, 4),
        }
