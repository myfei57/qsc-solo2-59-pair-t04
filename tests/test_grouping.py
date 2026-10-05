"""按多项实测自动配组、缺口策略与组串/簇登记履历。"""

from __future__ import annotations

import pytest

from batterystore.errors import ConfigError, InterlockError
from batterystore.grouping import (
    GapPolicy,
    GroupingLedger,
    GroupTolerance,
    UnitMeasurement,
    default_tolerance,
    match_groups,
)
from batterystore.store.jsonfile import JsonStore


def measurement(unit_id: str, capacity_ah: float = 280.0, resistance_mohm: float = 0.45,
                ocv_v: float = 3.330, self_discharge_pct: float = 2.0, batch_id: str = "B1") -> UnitMeasurement:
    return UnitMeasurement(unit_id, capacity_ah, resistance_mohm, ocv_v, self_discharge_pct, batch_id=batch_id)


def tolerance(group_size: int = 4) -> GroupTolerance:
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


def test_conforming_group_respects_all_four_metrics():
    # 容量都一致，但内阻越限：只看容量会误配，四项联判必须拒掉。
    rows = [
        measurement("c1", resistance_mohm=0.40),
        measurement("c2", resistance_mohm=0.41),
        measurement("c3", resistance_mohm=0.42),
        measurement("c4", resistance_mohm=0.50),  # 相对最低内阻超出 5% 窗口
    ]
    plan = match_groups(rows, tolerance(4), GapPolicy.HOLD_NEXT_BATCH)
    assert plan.groups == ()
    assert set(plan.waiting_units) == {"c1", "c2", "c3", "c4"}

    tight = [measurement(f"t{i}", resistance_mohm=0.40 + i * 0.001) for i in range(4)]
    ok = match_groups(tight, tolerance(4), GapPolicy.HOLD_NEXT_BATCH)
    assert len(ok.groups) == 1
    assert ok.groups[0].conforming


def test_hold_leaves_short_remainder_for_next_batch():
    rows = [measurement(f"c{i}", capacity_ah=280.0 + (i % 3) * 0.3) for i in range(9)]
    rows += [measurement(f"x{i}", capacity_ah=298.0) for i in range(4)]
    plan = match_groups(rows, tolerance(4), GapPolicy.HOLD_NEXT_BATCH)
    assert plan.gap.policy is GapPolicy.HOLD_NEXT_BATCH
    assert plan.gap.conforming_groups >= 1
    # 次优单体不补位：零头留在待配池，short_by 说明少配了几整组。
    assert plan.gap.fill_groups == 0
    assert plan.waiting_units
    assert plan.gap.short_by >= 1
    assert "等下一批" in plan.gap.consequence


def test_fill_suboptimal_marks_waived_group_and_cost():
    # 3 只主流单体 + 5 只明显偏高单体：合格组配不齐，补位强行凑两组。
    rows = [measurement(f"lo{i}", capacity_ah=280.0) for i in range(3)]
    rows += [measurement(f"hi{i}", capacity_ah=296.0) for i in range(5)]
    plan = match_groups(rows, tolerance(4), GapPolicy.FILL_SUBOPTIMAL)
    assert plan.gap.policy is GapPolicy.FILL_SUBOPTIMAL
    fill_groups = [group for group in plan.groups if group.suboptimal_fill]
    assert fill_groups
    fill = fill_groups[0]
    assert "capacity_ah" in fill.violated_limits
    assert all(unit.waived for unit in fill.units)
    assert "返工" in plan.gap.consequence


def test_hard_limited_units_are_never_mounted_even_when_filling():
    rows = [measurement(f"c{i}", capacity_ah=280.0) for i in range(7)]
    rows.append(measurement("weak", capacity_ah=90.0))
    rows.append(measurement("hot_r", capacity_ah=280.0, resistance_mohm=2.0))
    plan = match_groups(rows, tolerance(4), GapPolicy.FILL_SUBOPTIMAL)
    assert plan.blocked["weak"] == "capacity_below_min"
    assert plan.blocked["hot_r"] == "resistance_above_max"
    used = {unit.unit_id for group in plan.groups for unit in group.units}
    assert not ({"weak", "hot_r"} & used)


def test_invalid_tolerance_is_rejected():
    with pytest.raises(ConfigError):
        GroupTolerance(group_size=0, max_capacity_spread_pct=1.0, max_resistance_spread_pct=1.0,
                       max_ocv_spread_pct=1.0, max_self_discharge_spread_pct=1.0)
    with pytest.raises(ValueError):
        GapPolicy.resolve("ship_anyway")


def test_ledger_mounts_groups_and_blocks_double_assignment(tmp_path):
    ledger = GroupingLedger(JsonStore(str(tmp_path / "state")))
    rows = [measurement(f"c{i}", capacity_ah=280.0 + i * 0.2) for i in range(8)]
    ledger.admit(rows)
    result = ledger.auto_group(tolerance(4), GapPolicy.HOLD_NEXT_BATCH)
    group_ids = result["registered_group_ids"]
    assert len(group_ids) == 2
    ledger.mount_rack("grk-1", group_ids)

    # 已装簇的组串不能挂第二个簇。
    with pytest.raises(InterlockError):
        ledger.mount_rack("grk-2", group_ids[:1])
    # 同一簇标识不能重复装配。
    with pytest.raises(InterlockError):
        ledger.mount_rack("grk-1", group_ids[:1])

    snap = ledger.snapshot()
    assert snap["assembled_racks"] == 1
    assert snap["active_groups"] == 2
    assert all(ledger.trace_unit(unit_id)["status"] == "mounted" for unit_id in
               [row.unit_id for row in rows])
    assert ledger.trace_unit("c0")["current_rack_id"] == "grk-1"


def test_reassign_dissolves_group_and_keeps_full_trace(tmp_path):
    ledger = GroupingLedger(JsonStore(str(tmp_path / "state")))
    ledger.admit([measurement(f"c{i}", capacity_ah=280.0 + i * 0.2) for i in range(4)])
    grouped = ledger.auto_group(tolerance(4), GapPolicy.HOLD_NEXT_BATCH)
    group_id = grouped["registered_group_ids"][0]
    ledger.mount_rack("grk-1", [group_id])

    # 挂在簇上必须先拆簇。
    with pytest.raises(InterlockError):
        ledger.reassign_unit("c1")
    ledger.dismantle_rack("grk-1")
    outcome = ledger.reassign_unit("c1")
    assert outcome["group_id"] == group_id
    assert set(outcome["returned_unit_ids"]) == {"c0", "c1", "c2", "c3"}

    trace = ledger.trace_unit("c1")
    assert trace["current_group_id"] is None
    actions = [event["action"] for event in trace["trace"]]
    assert actions == ["admit", "group_bound", "rack_mounted", "rack_dismantled", "unit_reassigned"]

    # 旧组串已作废但历史仍可查；重新配组后 c1 只挂一个新组。
    assert ledger.groups(include_dissolved=True)[0]["status"] == "dissolved"
    ledger.admit([measurement(f"n{i}", capacity_ah=281.0 + i * 0.2) for i in range(3)])
    regrouped = ledger.auto_group(tolerance(4), GapPolicy.HOLD_NEXT_BATCH)
    new_group = regrouped["registered_group_ids"][0]
    current = ledger.trace_unit("c1")
    assert current["current_group_id"] == new_group
    assert current["status"] == "grouped"
    # 任意单体在有效组中只出现一次。
    membership: dict[str, int] = {}
    for group in ledger.groups():
        for unit_id in group["unit_ids"]:
            membership[unit_id] = membership.get(unit_id, 0) + 1
    assert all(count == 1 for count in membership.values())


def test_ledger_persists_relations_across_restart(tmp_path):
    root = str(tmp_path / "state")
    first = GroupingLedger(JsonStore(root))
    first.admit([measurement(f"c{i}", capacity_ah=280.0 + i * 0.2) for i in range(4)])
    grouped = first.auto_group(tolerance(4), GapPolicy.HOLD_NEXT_BATCH, ts=10.0)
    first.mount_rack("grk-1", grouped["registered_group_ids"], ts=11.0)

    reopened = GroupingLedger(JsonStore(root))
    assert reopened.snapshot()["assembled_racks"] == 1
    assert reopened.trace_unit("c2")["current_rack_id"] == "grk-1"
    assert {event["action"] for event in reopened.trace_unit("c2")["trace"]} >= {
        "admit", "group_bound", "rack_mounted"
    }


def test_default_tolerance_builds_usable_window():
    assert default_tolerance().group_size == 13
