"""V8.2.4 超额多情景自检。仅用于隔离测试库。"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import store


@pytest.fixture(autouse=True)
def isolated_empty_test_db():
    store.reset_all_for_tests()


@pytest.fixture
def c():
    return TestClient(app, raise_server_exceptions=False)


def case_headers(response):
    return {
        "X-Session-Id": response.headers["X-Session-Id"],
        "X-Project-Token": response.headers["X-Project-Token"],
    }


def create_blank(c, name="测试企业"):
    r = c.post("/api/cases/empty", params={"name": name})
    assert r.status_code == 200, r.text[:220]
    return r, case_headers(r)


def add_powerdrop_project(c, h, *, baseline_kwh=None, om=None, name="测试设备", power=100, hours=4000, drop=20, inv=100000):
    body = {"name": name, "device_type": "球磨机", "power_kw": power, "hours": hours}
    if baseline_kwh is not None:
        body["baseline_kwh"] = baseline_kwh
    d = c.post("/api/devices", headers=h, json=body)
    assert d.status_code == 200, d.text[:220]
    did = d.json()["case"]["devices"][-1]["id"]
    rf = {
        "name": f"{drop}kW降功率改造", "device_id": did, "power_drop_kw": drop,
        "investment_cny": inv, "select": True,
    }
    if om is not None:
        rf["annual_maintenance_cny"] = om
    r = c.post("/api/retrofits", headers=h, json=rf)
    assert r.status_code == 200, r.text[:220]
    t = c.put("/api/scenario", headers=h, json={"hours": hours, "energy_price": 0.8})
    assert t.status_code == 200, t.text[:220]
    return did, r.json()["case"]["scenario"]["selected_measure_id"]


def test_switch_a_b_a_then_public_demo(c):
    a, h = create_blank(c, "企业A")
    aid = a.json()["case"]["case_id"]
    atok = h["X-Project-Token"]
    b = c.post("/api/cases/empty", headers=h, params={"name": "企业B"})
    assert b.status_code == 200
    bid = b.json()["case"]["case_id"]
    btok = b.headers["X-Project-Token"]
    assert bid != aid and btok != atok
    assert b.json()["results"]["case"]["case_id"] == bid

    back = c.post(f"/api/cases/{aid}/activate", headers={**h, "X-Project-Token": btok})
    assert back.status_code == 200
    assert back.json()["case"]["case_id"] == aid
    assert back.json()["results"]["case"]["case_id"] == aid
    assert back.headers["X-Project-Token"] == atok

    demo = c.post("/api/reset-demo", headers={**h, "X-Project-Token": atok})
    assert demo.status_code == 200
    assert demo.json()["results"]["case"]["case_id"] == store.JINHUI_ID
    assert demo.json().get("project_token") in ("", None)
    assert demo.headers.get("X-Project-Token") in ("", None)
    no_tok = {"X-Session-Id": h["X-Session-Id"]}
    assert c.get("/api/case", headers=no_tok).json()["case_id"] == store.JINHUI_ID


def test_explicit_zero_survives_assume_zero_toggle(c):
    _, h = create_blank(c)
    add_powerdrop_project(c, h, om=0)
    assert c.put("/api/scenario", headers=h, json={"assume_zero_maintenance": True}).status_code == 200
    assert c.put("/api/scenario", headers=h, json={"assume_zero_maintenance": False}).status_code == 200
    case = c.get("/api/case", headers=h).json()
    sa = c.get("/api/results", headers=h).json()["selected_assessment"]
    assert case["retrofits"][0]["annual_maintenance_cny"] == 0
    assert sa["annual_maintenance_cny"] == 0
    assert sa["simple_payback_years"] == pytest.approx(1.56)


def test_assume_zero_then_uncheck_export_save_restore(c):
    r0, h = create_blank(c, "串联企业")
    add_powerdrop_project(c, h, om=None)
    assert c.put("/api/scenario", headers=h, json={"assume_zero_maintenance": True}).status_code == 200
    assert c.get("/api/results", headers=h).json()["selected_assessment"]["simple_payback_years"] is not None
    assert c.put("/api/scenario", headers=h, json={"assume_zero_maintenance": False}).status_code == 200
    sa = c.get("/api/results", headers=h).json()["selected_assessment"]
    assert sa["simple_payback_years"] is None
    assert sa["annual_maintenance_cny"] is None
    exp = c.post("/api/reports/export", headers=h, json={"preset": "full", "format": "txt"})
    assert exp.status_code == 200
    saved = c.post("/api/versions/save?label=after-uncheck", headers=h)
    assert saved.status_code == 200, saved.text[:200]
    vid = saved.json()["version_id"]
    # temporarily assume zero then restore the saved version
    assert c.put("/api/scenario", headers=h, json={"assume_zero_maintenance": True}).status_code == 200
    restored = c.post(f"/api/versions/{vid}/restore", headers=h)
    assert restored.status_code == 200, restored.text[:200]
    sa2 = c.get("/api/results", headers=h).json()["selected_assessment"]
    case = c.get("/api/case", headers=h).json()
    assert case["scenario"]["assume_zero_maintenance"] is False
    assert case["retrofits"][0]["annual_maintenance_cny"] is None
    assert sa2["simple_payback_years"] is None


def test_bind_device_then_recommendable(c):
    _, h = create_blank(c)
    raw = (
        "项目名称,目标设备,功率降低_kW,投资_元,年维护费_元\n"
        "未绑定技改,稍后绑定设备,20,100000,0\n"
    ).encode("utf-8-sig")
    up = c.post(
        "/api/upload/commit?kind=retrofit", headers=h,
        files={"file": ("retrofits.csv", raw, "text/csv")},
    )
    assert up.status_code == 200, up.text[:220]
    c.put("/api/scenario", headers=h, json={"hours": 4000, "energy_price": 0.8})
    sa = c.get("/api/results", headers=h).json()["selected_assessment"]
    assert sa.get("recommendable") is False
    mid = c.get("/api/case", headers=h).json()["retrofits"][0]["id"]
    d = c.post("/api/devices", headers=h, json={
        "name": "稍后绑定设备", "device_type": "球磨机", "power_kw": 100, "hours": 4000,
    })
    assert d.status_code == 200
    did = d.json()["case"]["devices"][-1]["id"]
    edited = c.put(f"/api/retrofits/{mid}", headers=h, json={"device_id": did, "select": True})
    assert edited.status_code == 200, edited.text[:220]
    t = c.put("/api/scenario", headers=h, json={"hours": 4000, "energy_price": 0.8, "selected_measure_id": mid})
    assert t.status_code == 200, t.text[:220]
    sa2 = c.get("/api/results", headers=h).json()["selected_assessment"]
    assert sa2.get("device_id") == did
    assert sa2.get("saved_kwh") == pytest.approx(80000)
    assert sa2.get("recommendable") is True


@pytest.mark.parametrize("idx", range(50))
def test_savings_exceed_baseline_sample(c, idx):
    baseline = 1000 + idx * 137
    drop = 15 + (idx % 10)
    hours = 3000 + (idx % 20) * 50
    saved = drop * hours
    assert saved > baseline
    _, h = create_blank(c, f"矛盾{idx}")
    add_powerdrop_project(c, h, baseline_kwh=baseline, om=0, power=200, hours=hours, drop=drop)
    sa = c.get("/api/results", headers=h).json()["selected_assessment"]
    assert sa["saved_kwh"] == pytest.approx(saved)
    assert sa.get("recommendable") is False
    assert sa.get("recommendation_blocked") is True
    assert sa.get("consistency_warnings")


def test_savings_exceed_baseline_bulk_500():
    """不走 HTTP，直接用计算层覆盖 500 组矛盾情景。"""
    from app.models.schema import CaseState, Device, RetrofitProject, ScenarioParams
    from app.services.compute import recalculate_case

    failures = []
    for i in range(500):
        baseline = 800 + i * 17
        drop = 10 + (i % 15)
        hours = 2500 + (i % 30) * 80
        saved = drop * hours
        if saved <= baseline:
            hours = 8000
            saved = drop * hours
        case = CaseState(
            case_id=f"bulk-{i}",
            name="bulk",
            is_demo=False,
            protected=False,
            devices=[Device(id="d1", name="磨", device_type="球磨机", power_kw=400, hours=hours, baseline_kwh=baseline)],
            retrofits=[RetrofitProject(
                id="r1", name="降功率", target_device="磨", device_id="d1",
                power_drop_kw=drop, investment_cny=100000, annual_maintenance_cny=0,
            )],
            scenario=ScenarioParams(
                hours=hours, energy_price=0.8, selected_measure_id="r1",
                power_drop_kw=drop, investment_cny=100000, annual_maintenance_cny=0,
                assume_zero_maintenance=False, emission_factor=0.4471,
            ),
        )
        sa = recalculate_case(case)["selected_assessment"]
        if sa.get("recommendable") is True or not sa.get("consistency_warnings"):
            failures.append((i, saved, baseline, sa.get("recommendable"), sa.get("consistency_warnings")))
        if sa.get("saved_kwh") != pytest.approx(saved):
            failures.append((i, "saved_changed", sa.get("saved_kwh"), saved))
    assert not failures, failures[:5]


def test_request_isolation_not_broken(c):
    a, ha = create_blank(c, "隔离甲")
    add_powerdrop_project(c, ha, om=0, name="甲设备")
    b, hb = create_blank(c, "隔离乙")
    add_powerdrop_project(c, hb, om=0, name="乙设备")
    ca = c.get("/api/case", headers=ha).json()
    cb = c.get("/api/case", headers=hb).json()
    assert ca["case_id"] != cb["case_id"]
    assert ca["devices"][0]["name"] == "甲设备"
    assert cb["devices"][0]["name"] == "乙设备"


def test_report_snapshot_and_archive_after_rename(c):
    _, h = create_blank(c, "报告企业")
    did, _ = add_powerdrop_project(c, h, om=0, name="原设备名")
    old = c.get("/api/results", headers=h).json()
    old_hash = old["assessment_snapshot"]["snapshot_id"]
    exp1 = c.post("/api/reports/export", headers=h, json={"preset": "full", "format": "txt"})
    assert exp1.status_code == 200
    edited = c.put(f"/api/devices/{did}", headers=h, json={"name": "改名后的设备"})
    assert edited.status_code == 200
    new = c.get("/api/results", headers=h).json()
    assert old_hash != new["assessment_snapshot"]["snapshot_id"]
    exp2 = c.post("/api/reports/export", headers=h, json={"preset": "full", "format": "txt"})
    assert exp2.status_code == 200
    archives = c.get("/api/reports/archives", headers=h).json()["reports"]
    assert len(archives) == 2
    ids = [a.get("export_id") for a in archives]
    assert len(set(ids)) == 2


def test_health_version_is_824(c):
    h = c.get("/api/health").json()
    assert h["version"] == "8.2.10"
    assert h["frontend_expected"] == "8.2.10"
