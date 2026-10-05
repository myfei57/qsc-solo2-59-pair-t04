"""平台层的配组接线：录入、自动配组、缺口代价、HTTP 接口与审计留痕。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _rows(prefix: str, count: int, capacity_ah: float) -> list[dict]:
    return [
        {
            "unit_id": f"{prefix}-{i:02d}",
            "capacity_ah": capacity_ah + (i % 3) * 0.2,
            "resistance_mohm": 0.45,
            "ocv_v": 3.330,
            "self_discharge_pct": 2.0,
            "batch_id": "B1",
        }
        for i in range(count)
    ]


def test_platform_hold_groups_and_records_gap(platform):
    platform.admit_units(_rows("c", 7, 280.0))
    result = platform.group_units("hold_next_batch", {"group_size": 4})
    assert result["gap"]["conforming_groups"] == 1
    assert result["gap"]["fill_groups"] == 0
    assert len(result["waiting_units"]) == 3
    assert result["gap"]["policy"] == "hold_next_batch"
    assert "等下一批" in result["gap"]["consequence"]
    assert len(platform.grouping_groups()) == 1


def test_platform_fill_suboptimal_carries_waiver(platform):
    platform.admit_units(_rows("lo", 3, 280.0) + _rows("hi", 5, 296.0))
    result = platform.group_units("fill_suboptimal", {"group_size": 4})
    assert result["gap"]["fill_groups"] == 1
    assert result["registered_group_ids"]
    actions = [entry["action"] for entry in platform.audit.query(component="grouping")]
    assert "units_grouped" in actions


def test_platform_mount_and_reassign_traceable(platform):
    platform.admit_units(_rows("c", 8, 280.0))
    grouped = platform.group_units("hold_next_batch", {"group_size": 4})
    ids = grouped["registered_group_ids"]
    platform.mount_group_rack("grk-1", ids)
    assert platform.trace_grouping_unit("c-00")["current_rack_id"] == "grk-1"
    platform.dismantle_group_rack("grk-1")
    platform.reassign_grouping_unit("c-01")
    trace = platform.trace_grouping_unit("c-01")
    assert [event["action"] for event in trace["trace"]][-1] == "unit_reassigned"
    platform.admit_units(_rows("n", 3, 281.0))
    platform.group_units("hold_next_batch", {"group_size": 4})
    memberships = [unit_id for group in platform.grouping_groups() for unit_id in group["unit_ids"]]
    assert len(memberships) == len(set(memberships))


def test_grouping_http_endpoints(platform):
    from batterystore.console.pages import PageLibrary
    from batterystore.console.api import Api

    api = Api(platform, PageLibrary("web"))
    status, admitted = api.handle("POST", "/api/grouping/admit", {}, {"units": _rows("c", 4, 280.0)})
    assert status == 200 and admitted["pool_size"] == 4
    status, run = api.handle(
        "POST", "/api/grouping/run", {}, {"policy": "hold_next_batch", "group_size": 4}
    )
    assert status == 200 and run["gap"]["conforming_groups"] == 1
    status, snap = api.handle("GET", "/api/grouping", {}, {})
    assert status == 200 and snap["active_groups"] == 1
    status, traced = api.handle("GET", "/api/grouping/unit/c-00", {}, {})
    assert status == 200 and traced["unit_id"] == "c-00"


def test_group_cli_admit_run(tmp_path):
    root = tmp_path / "cli-state"
    measurements = json.dumps(_rows("cli", 4, 280.0), ensure_ascii=False)
    common = [sys.executable, "-m", "batterystore", "--storage-root", str(root)]
    env_path = str(Path(__file__).resolve().parents[1])

    admitted = subprocess.run(
        [*common, "group", "--action", "admit", "--measurements", measurements],
        capture_output=True, text=True, env={"PYTHONPATH": env_path, "PATH": "/usr/bin:/bin"}, check=False,
    )
    assert admitted.returncode == 0, admitted.stderr
    run = subprocess.run(
        [*common, "group", "--action", "run", "--policy", "hold_next_batch", "--group-size", "4"],
        capture_output=True, text=True, env={"PYTHONPATH": env_path, "PATH": "/usr/bin:/bin"}, check=False,
    )
    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)
    assert payload["gap"]["conforming_groups"] == 1
