"""自动配组：多项实测规划、缺口处理与组套台账。"""

from __future__ import annotations

import json
import urllib.request

import pytest

from batterystore.config import MatchingLimits, StationConfig, config_to_mapping, default_config
from batterystore.console.server import ConsoleServer
from batterystore.errors import NamespaceError
from batterystore.matching import CellMeasurement, GroupRegistry, plan_groups

LIMITS = MatchingLimits(group_size=4, capacity_spread_ah=2.0, resistance_spread_mohm=0.2, voltage_spread_mv=20.0)


def _row(cell_id: str, capacity_ah: float, resistance_mohm: float = 0.15, voltage_v: float = 3.3) -> dict:
    return {
        "cell_id": cell_id,
        "capacity_ah": capacity_ah,
        "resistance_mohm": resistance_mohm,
        "voltage_v": voltage_v,
    }


def _rows(prefix: str, count: int, base: float = 280.0, step: float = 0.1) -> list[dict]:
    return [_row(f"{prefix}{index:02d}", base + index * step) for index in range(count)]


def _get(base_url: str, path: str):
    with urllib.request.urlopen(f"{base_url}{path}", timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _post(base_url: str, path: str, payload: dict):
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_plan_forms_groups_within_tolerance():
    plan = plan_groups([CellMeasurement.from_mapping(row) for row in _rows("c", 8)], LIMITS)
    assert len(plan.groups) == 2
    assert plan.deferred == ()
    assert plan.shortfall == 0
    for group in plan.groups:
        spreads = group.spreads()
        assert spreads["capacity_ah"] <= LIMITS.capacity_spread_ah
        assert spreads["resistance_mohm"] <= LIMITS.resistance_spread_mohm
        assert spreads["voltage_mv"] <= LIMITS.voltage_spread_mv
        assert len(group.cells) == LIMITS.group_size


def test_plan_defers_outliers_and_explains_shortfall():
    rows = _rows("c", 8)
    rows.append(_row("weak-cap", 260.0))
    rows.append(_row("weak-res", 280.5, resistance_mohm=0.9))
    rows.append(_row("weak-volt", 280.6, voltage_v=3.36))
    plan = plan_groups([CellMeasurement.from_mapping(row) for row in rows], LIMITS, requested_groups=3)
    reasons = {cell.measurement.cell_id: cell.reason for cell in plan.deferred}
    assert reasons["weak-cap"] == "capacity_spread"
    assert reasons["weak-res"] == "resistance_spread"
    assert reasons["weak-volt"] == "voltage_spread"
    assert len(plan.groups) == 2
    assert plan.shortfall == 1
    payload = plan.as_dict()
    assert payload["policy"] == "defer_to_next_batch"
    assert "等下一批" in payload["policy_note"]
    assert "1 组" in payload["policy_note"]
    assert all(group.spreads()["capacity_ah"] <= LIMITS.capacity_spread_ah for group in plan.groups)


def test_plan_marks_tail_cells_short_of_group():
    plan = plan_groups([CellMeasurement.from_mapping(row) for row in _rows("c", 9)], LIMITS)
    assert len(plan.groups) == 2
    assert [cell.reason for cell in plan.deferred] == ["short_of_group"]
    assert plan.deferred[0].as_dict()["note"]


def test_plan_rejects_invalid_input():
    with pytest.raises(ValueError):
        plan_groups([], LIMITS)
    duplicated = _rows("c", 4) + _rows("c", 2)
    with pytest.raises(ValueError):
        plan_groups([CellMeasurement.from_mapping(row) for row in duplicated], LIMITS)
    with pytest.raises(ValueError):
        plan_groups([CellMeasurement.from_mapping(row) for row in _rows("c", 4)], LIMITS, requested_groups=-1)


def test_measurement_validation():
    with pytest.raises(ValueError):
        CellMeasurement(cell_id="", capacity_ah=280.0, resistance_mohm=0.15, voltage_v=3.3)
    with pytest.raises(ValueError):
        CellMeasurement.from_mapping(_row("c1", -1.0))
    with pytest.raises(ValueError):
        CellMeasurement.from_mapping({"cell_id": "c1"})
    with pytest.raises(ValueError):
        CellMeasurement.from_mapping("not-a-mapping")


def test_registry_enforces_single_membership():
    registry = GroupRegistry()
    registry.register("g1", "rk-01-mg1", "rk-01", ["c1", "c2", "c3"])
    with pytest.raises(NamespaceError):
        registry.register("g2", "rk-01-mg2", "rk-01", ["c3", "c4"])
    with pytest.raises(NamespaceError):
        registry.register("g1", "rk-01-mg9", "rk-01", ["c9"])
    with pytest.raises(NamespaceError):
        registry.register("g3", "rk-01-mg3", "rk-01", [])
    assert registry.group_of("c3")["group_id"] == "g1"
    assert registry.members("g1") == ["c1", "c2", "c3"]


def test_registry_detach_keeps_lineage():
    registry = GroupRegistry()
    registry.register("g1", "rk-01-mg1", "rk-01", ["c1", "c2"])
    change = registry.detach("c1", "repair")
    assert change["action"] == "detach"
    assert change["group_id"] == "g1"
    registry.register("g2", "rk-02-mg1", "rk-02", ["c1", "c9"])
    history = registry.history_of("c1")
    assert [item["action"] for item in history] == ["register", "detach", "register"]
    current = registry.group_of("c1")
    assert current["group_id"] == "g2"
    assert current["rack_id"] == "rk-02"
    assert "c1" not in registry.members("g1")
    assert registry.snapshot()["epoch"] == 3
    with pytest.raises(NamespaceError):
        registry.detach("not-registered")
    with pytest.raises(NamespaceError):
        registry.group_of("not-registered")


def test_platform_matching_registers_detaches_and_replans(platform):
    result = platform.plan_matching(_rows("c", 13), requested_groups=1)
    assert result["ok"]
    assert result["plan"]["formed_groups"] == 1
    assert result["plan"]["shortfall"] == 0
    assert len(result["plan"]["deferred"]) == 1
    change = result["registered"][0]
    assert change["rack_id"] == "rk-01"
    assert change["pack_id"] == "rk-01-mg1"
    snapshot = platform.matching_snapshot()
    assert len(snapshot["cells"]) == platform.config.matching.group_size

    # 在册单体不能直接拿去重配，必须先拆，杜绝一只单体同时挂两组。
    with pytest.raises(NamespaceError):
        platform.plan_matching(_rows("c", 12))

    cell = change["cells"][0]
    detached = platform.detach_cell(cell, "repair")
    assert detached["action"] == "detach"
    lineage = platform.cell_grouping(cell)
    assert lineage["current"] is None
    assert [item["action"] for item in lineage["history"]] == ["register", "detach"]

    # 拆下的单体随下一批重配进新组，原组套关系仍然可查。
    replanned = platform.plan_matching([_row(cell, 280.0)] + _rows("n", 11), requested_groups=1)
    assert replanned["plan"]["formed_groups"] == 1
    lineage = platform.cell_grouping(cell)
    assert lineage["current"]["group_id"] != change["group_id"]
    assert lineage["current"]["pack_id"] == "rk-01-mg2"
    assert [item["action"] for item in lineage["history"]] == ["register", "detach", "register"]
    assert cell in lineage["current"]["members"]


def test_matching_limits_roundtrip_through_config():
    config = default_config()
    assert config.matching.group_size >= 2
    assert config.matching.capacity_spread_ah > 0
    raw = config_to_mapping(config)
    assert raw["limits"]["matching"]["group_size"] == config.matching.group_size
    reloaded = StationConfig.from_mapping(raw)
    assert reloaded.matching == config.matching


def test_console_matching_endpoints(platform):
    server = ConsoleServer(platform, port=0)
    base_url = server.start()
    try:
        code, body = _post(base_url, "/api/matching/plan", {"measurements": _rows("c", 13), "requested_groups": 1})
        assert code == 200
        assert body["plan"]["formed_groups"] == 1
        cell = body["registered"][0]["cells"][0]
        code, trace = _get(base_url, f"/api/matching/cell/{cell}")
        assert code == 200
        assert trace["current"]["pack_id"] == "rk-01-mg1"
        code, change = _post(base_url, "/api/matching/detach", {"cell_id": cell, "reason": "repair"})
        assert code == 200
        assert change["action"] == "detach"
        code, trace = _get(base_url, f"/api/matching/cell/{cell}")
        assert trace["current"] is None
        assert len(trace["history"]) == 2
        code, snapshot = _get(base_url, "/api/matching")
        assert code == 200
        assert snapshot["epoch"] == 2
    finally:
        server.stop()
