"""配组登记台账：组串、簇与履历。

配出来的组不能只活在一次返回值里，必须落到三级关系上：

* **单体 → 组串**：每只单体当前最多挂一个组串；
* **组串 → 簇**：每个组串当前最多挂一个簇；
* **履历**：绑定、装簇、拆串、拆簇、拆下重配都追加一条不可变流水，旧关系不删。

唯一性靠两张"当前归属"表在同一把锁内校验：提交配组前所有单体必须处于待配池，
装簇前所有组串必须处于已绑定未装簇状态。单体被拆下来重配时，其原组串因缺员整
串作废、其余单体回池重配，但旧的绑定流水仍在，``trace_unit`` 能把"这只单体
先后待过哪一串、哪一簇"完整还原。台账每次变更都原子落盘，重启后关系连续。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from ..errors import InterlockError
from ..store.jsonfile import JsonStore
from .match import MatchPlan, match_groups
from .policy import GapPolicy
from .tolerance import GroupTolerance
from .units import Unit, UnitMeasurement

DOC_ID = "grouping_ledger"
COMPONENT = "grouping"


@dataclass
class GroupRecord:
    """一个已登记组串。"""

    group_id: str
    unit_ids: list[str]
    batch_id: str
    spreads_pct: dict[str, float]
    violated_limits: list[str]
    suboptimal_fill: bool
    status: str = "active"
    rack_id: str | None = None
    created_ts: float = 0.0

    def as_dict(self) -> dict:
        return {
            "group_id": self.group_id,
            "unit_ids": list(self.unit_ids),
            "batch_id": self.batch_id,
            "spreads_pct": dict(self.spreads_pct),
            "violated_limits": list(self.violated_limits),
            "suboptimal_fill": self.suboptimal_fill,
            "conforming": self.status == "active" and not self.suboptimal_fill,
            "status": self.status,
            "rack_id": self.rack_id,
            "created_ts": round(self.created_ts, 3),
        }


@dataclass
class RackRecord:
    """一个由组串装成的簇。"""

    rack_id: str
    group_ids: list[str]
    status: str = "assembled"
    created_ts: float = 0.0

    def as_dict(self) -> dict:
        return {
            "rack_id": self.rack_id,
            "group_ids": list(self.group_ids),
            "status": self.status,
            "created_ts": round(self.created_ts, 3),
        }


class GroupingLedger:
    """配组关系台账（内存表 + 原子落盘）。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store
        self._lock = threading.RLock()
        self._units: dict[str, Unit] = {}
        self._groups: dict[str, GroupRecord] = {}
        self._racks: dict[str, RackRecord] = {}
        self._history: list[dict] = []
        self._group_seq = 0
        self._restore()

    # ----- 录入与查询 -------------------------------------------------

    def admit(self, measurements: list[UnitMeasurement], ts: float = 0.0) -> dict:
        """把一批分选实测录进待配池，已挂组的单体不允许覆盖录入。"""

        if not measurements:
            raise InterlockError("没有录入任何单体实测", component=COMPONENT)
        with self._lock:
            admitted: list[str] = []
            for measurement in measurements:
                existing = self._units.get(measurement.unit_id)
                if existing is not None and existing.status != "pool":
                    raise InterlockError(
                        f"单体 {measurement.unit_id} 当前挂在组上，不能重新录入", component=COMPONENT
                    )
                self._units[measurement.unit_id] = Unit(
                    measurement=measurement, status="pool", batch_id=measurement.batch_id
                )
                admitted.append(measurement.unit_id)
                self._history.append(
                    {"ts": ts, "unit_id": measurement.unit_id, "action": "admit", "batch_id": measurement.batch_id}
                )
            self._persist()
            return {"admitted": admitted, "pool_size": self._pool_size()}

    def pool_units(self) -> list[dict]:
        """返回待配池里的单体。"""

        with self._lock:
            return [self._units[unit_id].as_dict() for unit_id in sorted(self._units) if self._units[unit_id].status == "pool"]

    def groups(self, include_dissolved: bool = False) -> list[dict]:
        """返回组串登记。"""

        with self._lock:
            return [
                record.as_dict()
                for record in self._groups.values()
                if include_dissolved or record.status == "active"
            ]

    def racks(self, include_dissolved: bool = False) -> list[dict]:
        """返回簇登记。"""

        with self._lock:
            return [
                record.as_dict()
                for record in self._racks.values()
                if include_dissolved or record.status == "assembled"
            ]

    def trace_unit(self, unit_id: str) -> dict:
        """还原一只单体的全部组串/簇履历（含已作废关系）。"""

        with self._lock:
            if unit_id not in self._units:
                raise InterlockError(f"未录入的单体: {unit_id}", component=COMPONENT)
            current_group = self._current_group(unit_id)
            trace = [dict(event) for event in self._history if event.get("unit_id") == unit_id]
            return {
                "unit_id": unit_id,
                "status": self._units[unit_id].status,
                "current_group_id": current_group.group_id if current_group else None,
                "current_rack_id": current_group.rack_id if current_group else None,
                "trace": trace,
            }

    # ----- 配组 -------------------------------------------------------

    def preview(self, tolerance: GroupTolerance, policy: GapPolicy | str) -> dict:
        """只算不登记，供核对配组与缺口是否合意。"""

        with self._lock:
            return self._plan(tolerance, policy).as_dict()

    def auto_group(self, tolerance: GroupTolerance, policy: GapPolicy | str, ts: float = 0.0) -> dict:
        """配组并把合格组/补位组登记成组串，返回配组结果与登记到的组串号。"""

        with self._lock:
            plan = self._plan(tolerance, policy)
            batch_id = self._current_batch()
            registered: list[str] = []
            for matched in plan.groups:
                unit_ids = [unit.unit_id for unit in matched.units]
                for unit_id in unit_ids:
                    if self._units[unit_id].status != "pool":
                        raise InterlockError(
                            f"单体 {unit_id} 已挂在组上，禁止重复配组", component=COMPONENT
                        )
                self._group_seq += 1
                group_id = f"grp-{self._group_seq:04d}"
                self._groups[group_id] = GroupRecord(
                    group_id=group_id,
                    unit_ids=unit_ids,
                    batch_id=batch_id,
                    spreads_pct=matched.spreads_pct,
                    violated_limits=list(matched.violated_limits),
                    suboptimal_fill=matched.suboptimal_fill,
                    created_ts=ts,
                )
                for unit_id in unit_ids:
                    self._units[unit_id].status = "grouped"
                    self._history.append(
                        {
                            "ts": ts,
                            "unit_id": unit_id,
                            "group_id": group_id,
                            "action": "group_bound",
                            "suboptimal_fill": matched.suboptimal_fill,
                            "violated_limits": list(matched.violated_limits),
                        }
                    )
                registered.append(group_id)
            self._persist()
            return {**plan.as_dict(), "registered_group_ids": registered}

    # ----- 装簇与拆解 -------------------------------------------------

    def mount_rack(self, rack_id: str, group_ids: list[str], ts: float = 0.0) -> dict:
        """把若干组串装成一个簇，组串必须存在、有效且未挂别的簇。"""

        if not rack_id:
            raise InterlockError("簇标识不能为空", component=COMPONENT)
        if not group_ids:
            raise InterlockError("一个簇至少需要一个组串", component=COMPONENT)
        with self._lock:
            existing = self._racks.get(rack_id)
            if existing is not None and existing.status == "assembled":
                raise InterlockError(f"簇 {rack_id} 已在用，禁止重复装配", component=COMPONENT)
            records = [self._require_active_group(group_id) for group_id in group_ids]
            if len({group_id for group_id in group_ids}) != len(group_ids):
                raise InterlockError("同一组串在一个簇里出现了两次", component=COMPONENT)
            for record in records:
                if record.rack_id is not None:
                    raise InterlockError(
                        f"组串 {record.group_id} 已挂在簇 {record.rack_id}，禁止一挂两簇", component=COMPONENT
                    )
            rack = RackRecord(rack_id=rack_id, group_ids=list(group_ids), created_ts=ts)
            self._racks[rack_id] = rack
            unit_count = 0
            for record in records:
                record.rack_id = rack_id
                for unit_id in record.unit_ids:
                    self._units[unit_id].status = "mounted"
                    unit_count += 1
                    self._history.append(
                        {"ts": ts, "unit_id": unit_id, "group_id": record.group_id, "rack_id": rack_id,
                         "action": "rack_mounted"}
                    )
            self._persist()
            return {"rack_id": rack_id, "group_ids": list(group_ids), "units": unit_count}

    def dismantle_rack(self, rack_id: str, ts: float = 0.0) -> dict:
        """拆开一个簇，组串退回已绑定状态（关系流水保留）。"""

        with self._lock:
            rack = self._require_assembled_rack(rack_id)
            freed_groups = list(rack.group_ids)
            for group_id in freed_groups:
                record = self._groups[group_id]
                record.rack_id = None
                for unit_id in record.unit_ids:
                    self._units[unit_id].status = "grouped"
                    self._history.append(
                        {"ts": ts, "unit_id": unit_id, "group_id": group_id, "rack_id": rack_id,
                         "action": "rack_dismantled"}
                    )
            rack.status = "dissolved"
            self._persist()
            return {"rack_id": rack_id, "freed_group_ids": freed_groups}

    def dismantle_group(self, group_id: str, ts: float = 0.0) -> dict:
        """整串拆下作废，单体全部回待配池；挂在簇上时必须先拆簇。"""

        with self._lock:
            record = self._require_active_group(group_id)
            if record.rack_id is not None:
                raise InterlockError(
                    f"组串 {group_id} 仍挂在簇 {record.rack_id}，请先拆簇", component=COMPONENT
                )
            return self._dissolve_group(record, ts, action="group_dismantled")

    def reassign_unit(self, unit_id: str, ts: float = 0.0) -> dict:
        """把一只单体从当前组串拆下重配：原串缺员作废，串内其余单体一并回池。"""

        with self._lock:
            if unit_id not in self._units:
                raise InterlockError(f"未录入的单体: {unit_id}", component=COMPONENT)
            record = self._current_group(unit_id)
            if record is None:
                raise InterlockError(f"单体 {unit_id} 不在任何组串上，无需拆下", component=COMPONENT)
            if record.rack_id is not None:
                raise InterlockError(
                    f"单体 {unit_id} 所在组串仍挂在簇 {record.rack_id}，请先拆簇再拆下重配",
                    component=COMPONENT,
                )
            result = self._dissolve_group(record, ts, action="unit_reassigned", focus_unit=unit_id)
            result["reassigned_unit_id"] = unit_id
            return result

    def snapshot(self) -> dict:
        """返回台账当前关系快照。"""

        with self._lock:
            return {
                "pool_size": self._pool_size(),
                "active_groups": len([g for g in self._groups.values() if g.status == "active"]),
                "assembled_racks": len([r for r in self._racks.values() if r.status == "assembled"]),
                "conforming_groups": len(
                    [g for g in self._groups.values() if g.status == "active" and not g.suboptimal_fill]
                ),
                "waived_groups": len(
                    [g for g in self._groups.values() if g.status == "active" and g.suboptimal_fill]
                ),
                "history_events": len(self._history),
                "groups": self.groups(),
                "racks": self.racks(),
            }

    # ----- 内部实现 ---------------------------------------------------

    def _plan(self, tolerance: GroupTolerance, policy: GapPolicy | str) -> MatchPlan:
        measurements = [
            self._units[unit_id].measurement
            for unit_id in sorted(self._units)
            if self._units[unit_id].status == "pool"
        ]
        return match_groups(measurements, tolerance, policy)

    def _current_batch(self) -> str:
        batches = {unit.batch_id for unit in self._units.values() if unit.status == "grouped" and unit.batch_id}
        return sorted(batches)[0] if batches else ""

    def _pool_size(self) -> int:
        return len([unit for unit in self._units.values() if unit.status == "pool"])

    def _current_group(self, unit_id: str) -> GroupRecord | None:
        for record in self._groups.values():
            if record.status == "active" and unit_id in record.unit_ids:
                return record
        return None

    def _require_active_group(self, group_id: str) -> GroupRecord:
        record = self._groups.get(group_id)
        if record is None or record.status != "active":
            raise InterlockError(f"未知或已作废的组串: {group_id}", component=COMPONENT)
        return record

    def _require_assembled_rack(self, rack_id: str) -> RackRecord:
        rack = self._racks.get(rack_id)
        if rack is None or rack.status != "assembled":
            raise InterlockError(f"未知或已拆开的簇: {rack_id}", component=COMPONENT)
        return rack

    def _dissolve_group(
        self, record: GroupRecord, ts: float, *, action: str, focus_unit: str | None = None
    ) -> dict:
        returned = list(record.unit_ids)
        for unit_id in returned:
            self._units[unit_id].status = "pool"
            self._history.append(
                {"ts": ts, "unit_id": unit_id, "group_id": record.group_id, "action": action,
                 "focus": unit_id == focus_unit if focus_unit else False}
            )
        record.status = "dissolved"
        record.rack_id = None
        self._persist()
        return {"group_id": record.group_id, "returned_unit_ids": returned, "pool_size": self._pool_size()}

    def _persist(self) -> None:
        self._store.write(
            DOC_ID,
            {
                "group_seq": self._group_seq,
                "units": {unit_id: unit.as_dict() for unit_id, unit in self._units.items()},
                "measurements": {
                    unit_id: unit.measurement.as_dict() for unit_id, unit in self._units.items()
                },
                "groups": {gid: record.as_dict() for gid, record in self._groups.items()},
                "racks": {rid: rack.as_dict() for rid, rack in self._racks.items()},
                "history": list(self._history),
            },
        )

    def _restore(self) -> None:
        raw = self._store.read(DOC_ID)
        if not isinstance(raw, dict):
            return
        self._group_seq = int(raw.get("group_seq", 0))
        measured = raw.get("measurements", {})
        for unit_id, state in raw.get("units", {}).items():
            data = measured.get(unit_id, state)
            measurement = UnitMeasurement(
                unit_id=unit_id,
                capacity_ah=float(data["capacity_ah"]),
                resistance_mohm=float(data["resistance_mohm"]),
                ocv_v=float(data["ocv_v"]),
                self_discharge_pct=float(data["self_discharge_pct"]),
                batch_id=str(data.get("batch_id", "")),
                measured_ts=float(data.get("measured_ts", 0.0)),
            )
            self._units[unit_id] = Unit(measurement=measurement, status=str(state.get("status", "pool")))
        for gid, data in raw.get("groups", {}).items():
            self._groups[gid] = GroupRecord(
                group_id=gid,
                unit_ids=list(data["unit_ids"]),
                batch_id=str(data.get("batch_id", "")),
                spreads_pct=dict(data.get("spreads_pct", {})),
                violated_limits=list(data.get("violated_limits", [])),
                suboptimal_fill=bool(data.get("suboptimal_fill", False)),
                status=str(data.get("status", "active")),
                rack_id=data.get("rack_id"),
                created_ts=float(data.get("created_ts", 0.0)),
            )
        for rid, data in raw.get("racks", {}).items():
            self._racks[rid] = RackRecord(
                rack_id=rid,
                group_ids=list(data["group_ids"]),
                status=str(data.get("status", "assembled")),
                created_ts=float(data.get("created_ts", 0.0)),
            )
        self._history = [dict(event) for event in raw.get("history", [])]
