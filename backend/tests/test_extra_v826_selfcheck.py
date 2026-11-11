"""V8.2.6 超额多情景自检。仅用于隔离测试库。"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.schema import CaseState, Device, RetrofitProject, ScenarioParams
from app.services import store
from app.version import APP_VERSION, BUILD_ID


@pytest.fixture(autouse=True)
def isolated_reset():
    store.reset_all_for_tests()


@pytest.fixture
def c():
    return TestClient(app, raise_server_exceptions=False)


def project(c, name="超额企业826"):
    r = c.post("/api/cases/empty", json={"name": name})
    assert r.status_code == 200, r.text[:400]
    return r, {
        "X-Session-Id": r.headers["X-Session-Id"],
        "X-Project-Token": r.headers["X-Project-Token"],
    }


def csv_upload(c, h, kind, data):
    return c.post(
        f"/api/upload/commit?kind={kind}",
        headers=h,
        files={"file": ("data.csv", data.encode("utf-8-sig"), "text/csv")},
    )


def test_health_version_is_826(c):
    h = c.get("/api/health").json()
    assert h["version"] == "8.2.12" == APP_VERSION
    assert h["frontend_expected"] == "8.2.12"
    assert h["build_id"] == BUILD_ID


def test_two_clients_cannot_hijack_via_header_session_only(c):
    a = TestClient(app, raise_server_exceptions=False)
    ra = a.post("/api/cases/empty", json={"name": "甲私有"})
    assert ra.status_code == 200
    ha = {
        "X-Session-Id": ra.headers["X-Session-Id"],
        "X-Project-Token": ra.headers["X-Project-Token"],
    }
    assert a.post(
        "/api/devices",
        headers=ha,
        json={"name": "机密磨机", "device_type": "球磨机", "power_kw": 120, "hours": 4000},
    ).status_code == 200
    cid = ra.json()["case"]["case_id"]
    assert a.post("/api/reset-demo", headers=ha).status_code == 200
    assert a.post(
        "/api/reports/export",
        headers={"X-Session-Id": ha["X-Session-Id"]},
        json={"preset": "one_page", "format": "txt"},
    ).status_code == 200

    b = TestClient(app, raise_server_exceptions=False)
    public = b.get("/api/reports/archives")
    assert public.status_code == 200
    for row in public.json().get("reports", []):
        assert "owner_session" not in row or not row.get("owner_session")
    replay = b.post(f"/api/cases/{cid}/activate", headers={"X-Session-Id": ha["X-Session-Id"]})
    assert replay.status_code in (401, 403, 404)
    listed = b.get("/api/cases", headers={"X-Session-Id": ha["X-Session-Id"]}).json()["cases"]
    assert all(x.get("case_id") != cid for x in listed)


def test_legacy_dup_id_rebinds_by_target_name():
    old = CaseState(
        case_id="legacy",
        name="dup",
        devices=[
            Device(id="dev-dup", name="磨机A", device_type="球磨机", power_kw=100, hours=4000),
            Device(id="dev-dup", name="磨机B", device_type="球磨机", power_kw=180, hours=4000),
        ],
        retrofits=[
            RetrofitProject(
                id="rf1",
                name="磨机B改造",
                device_id="dev-dup",
                target_device="磨机B",
                power_drop_kw=20,
                investment_cny=1,
                annual_maintenance_cny=0,
            )
        ],
        scenario=ScenarioParams(selected_measure_id="rf1"),
    )
    store._dedupe_entity_ids(old)
    m = old.retrofits[0]
    matched = next(d for d in old.devices if d.id == m.device_id)
    assert matched.name == "磨机B"


def test_explicit_new_device_id_breaks_old_retrofit(c):
    _, h = project(c)
    assert csv_upload(
        c, h, "device", "设备名称,设备类型,功率_kW,年运行小时\n泵A,水泵,100,4000\n"
    ).status_code == 200
    old_id = c.get("/api/case", headers=h).json()["devices"][0]["id"]
    assert c.post(
        "/api/retrofits",
        headers=h,
        json={
            "name": "旧",
            "device_id": old_id,
            "power_drop_kw": 5,
            "investment_cny": 1,
            "annual_maintenance_cny": 0,
        },
    ).status_code == 200
    r = csv_upload(
        c,
        h,
        "device",
        "设备名称,设备ID,设备类型,功率_kW,年运行小时\n泵A,dev-brand-new-id-001,水泵,180,4000\n",
    )
    assert r.status_code == 200, r.text[:300]
    state = c.get("/api/case", headers=h).json()
    assert state["devices"][0]["id"] == "dev-brand-new-id-001"
    assert not any(m.get("device_id") == old_id for m in state["retrofits"])


def test_csv_type_change_invalidates_pump_reference(c):
    _, h = project(c)
    d = c.post(
        "/api/devices",
        headers=h,
        json={"name": "泵X", "device_type": "循环水泵", "power_kw": 55, "hours": 4000, "baseline_kwh": 200000},
    )
    did = d.json()["case"]["devices"][-1]["id"]
    r = c.post(
        "/api/retrofits",
        headers=h,
        json={
            "name": "水泵改造",
            "device_id": did,
            "method_code": "pump_hydraulic",
            "model_inputs": {
                "q_m3_h": 100,
                "head_m": 50,
                "eta_pump_before": 0.7,
                "eta_pump_after": 0.8,
                "eta_motor": 0.9,
                "hours": 4000,
            },
            "investment_cny": 50000,
            "annual_maintenance_cny": 0,
            "select": True,
        },
    )
    assert r.status_code == 200, r.text[:300]
    mid = r.json()["case"]["retrofits"][-1]["id"]
    assert c.post(
        "/api/references/select",
        headers=h,
        json={
            "source_id": "gb-19762-2025",
            "applicable_device_id": did,
            "method_code": "pump_hydraulic",
            "parameter_key": "eta_pump_before",
            "unit": "0-1",
            "choice": "match_trial",
            "apply_intent": "apply_to_trial",
            "parameter_value": 0.5,
            "measure_id": mid,
        },
    ).status_code == 200
    assert csv_upload(
        c,
        h,
        "device",
        "设备名称,设备类型,功率_kW,年运行小时,基准年电耗_kWh\n泵X,变压器,55,4000,200000\n",
    ).status_code == 200
    case = c.get("/api/case", headers=h).json()
    stale = [
        s
        for s in case["reference_selections"]
        if s["method_code"] == "pump_hydraulic" and (s["active"] or s["applied"])
    ]
    assert not stale


def test_archive_failure_sets_warning_header(c):
    _, h = project(c)
    with patch.object(store, "_persist_report_archive", side_effect=OSError("disk full")):
        r = c.post("/api/reports/export", headers=h, json={"preset": "one_page", "format": "txt"})
    assert r.headers.get("X-Archive-Saved") == "false" or r.headers.get("X-Archive-Warning") or r.status_code >= 400


def test_multi_enterprise_switch_and_restart_list(c):
    a, ha = project(c, "甲企")
    b = c.post("/api/cases/empty", headers=ha, json={"name": "乙企"})
    assert b.status_code == 200
    sid = ha["X-Session-Id"]
    tb = b.headers["X-Project-Token"]
    ck_sid, ck_mac = c.cookies.get("ltzs_sid"), c.cookies.get("ltzs_stk")
    code = """from fastapi.testclient import TestClient
from app.main import app
c=TestClient(app)
c.cookies.set("ltzs_sid", %r)
c.cookies.set("ltzs_stk", %r)
h={"X-Session-Id":%r,"X-Project-Token":%r}
r=c.get("/api/cases",headers=h)
print(__import__("json").dumps([x["name"] for x in r.json()["cases"]],ensure_ascii=False))
""" % (ck_sid, ck_mac, sid, tb)
    p = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=25,
        cwd=os.getcwd(),
    )
    assert p.returncode == 0, p.stderr[:400]
    names = json.loads(p.stdout.strip().splitlines()[-1])
    assert "甲企" in names and "乙企" in names


def test_restore_then_edit_persists(c):
    r, h = project(c, "恢复编辑")
    assert c.put("/api/scenario", headers=h, json={"energy_price": 0.91}).status_code == 200
    vid = c.post("/api/versions/save", headers=h, params={"label": "v1"}).json()["version_id"]
    assert c.put("/api/scenario", headers=h, json={"energy_price": 1.2}).status_code == 200
    assert c.post(f"/api/versions/{vid}/restore", headers=h).status_code == 200
    assert c.put("/api/scenario", headers=h, json={"energy_price": 0.77}).status_code == 200
    state = c.get("/api/case", headers=h).json()
    with sqlite3.connect(store.DB_PATH) as db:
        row = db.execute("SELECT payload FROM projects WHERE case_id=?", (state["case_id"],)).fetchone()
    assert json.loads(row[0])["scenario"]["energy_price"] == pytest.approx(0.77)


def test_same_name_different_explicit_ids(c):
    _, h = project(c)
    data = (
        "设备名称,设备ID,设备类型,功率_kW,年运行小时\n"
        "冷却泵,dev-alpha-1,水泵,60,4000\n"
        "冷却泵,dev-beta-2,水泵,80,4000\n"
    )
    assert csv_upload(c, h, "device", data).status_code == 200
    ids = [d["id"] for d in c.get("/api/case", headers=h).json()["devices"]]
    assert ids == ["dev-alpha-1", "dev-beta-2"]


def test_fullwidth_year_and_js_url_still_guarded(c):
    _, h = project(c)
    bad = csv_upload(c, h, "energy", "年份,工业用电量_万kWh\n2024,100\n２０２４,200\n")
    assert bad.status_code in (400, 409, 422)
    ok = csv_upload(
        c, h, "energy", "年份,工业用电量_万kWh,来源URL\n2025,100,javascript:alert(1)\n"
    )
    assert ok.status_code == 200
    years = c.get("/api/case", headers=h).json()["energy_years"]
    assert not str(years[0].get("url") or "").lower().startswith("javascript:")


def test_device_template_lists_device_id(c):
    kinds = {k["id"]: k["fields"] for k in c.get("/api/templates").json()["kinds"]}
    assert "设备ID" in kinds["device"]
    assert "设备ID" in kinds["retrofit"]
