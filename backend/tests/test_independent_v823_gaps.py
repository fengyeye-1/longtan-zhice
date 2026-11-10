# -*- coding: utf-8 -*-
"""
陇碳智策 V8.2.3：独立新增回归用例
⚠️ 会清空测试数据库！仅在原ZIP的独立临时解压副本运行，不得连接生产数据。
用法：在 <副本>/backend 目录下执行：
    python -m pytest -q tests/test_independent_v823_gaps.py
这些用例是独立新增的验收条件，原版V8.2.3存在预期失败，不应靠删测试消除红灯。
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

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


def case_headers(response) -> dict[str, str]:
    return {
        "X-Session-Id": response.headers["X-Session-Id"],
        "X-Project-Token": response.headers["X-Project-Token"],
    }


def create_blank(c, name="测试企业"):
    # 首次无Token创建；后续操作显式携带Token，模拟网页本地凭证。
    r = c.post("/api/cases/empty", params={"name": name})
    assert r.status_code == 200, r.text[:220]
    return r, case_headers(r)


def add_powerdrop_project(c, h, *, baseline_kwh=None, om=None, name="测试设备"):
    body = {"name": name, "device_type": "球磨机", "power_kw": 100, "hours": 4000}
    if baseline_kwh is not None:
        body["baseline_kwh"] = baseline_kwh
    d = c.post("/api/devices", headers=h, json=body)
    assert d.status_code == 200, d.text[:220]
    did = d.json()["case"]["devices"][-1]["id"]
    rf = {
        "name": "20kW降功率改造", "device_id": did, "power_drop_kw": 20,
        "investment_cny": 100000, "select": True,
    }
    if om is not None:
        rf["annual_maintenance_cny"] = om
    r = c.post("/api/retrofits", headers=h, json=rf)
    assert r.status_code == 200, r.text[:220]
    t = c.put("/api/scenario", headers=h, json={"hours": 4000, "energy_price": 0.8})
    assert t.status_code == 200, t.text[:220]
    return did, r.json()["case"]["scenario"]["selected_measure_id"]


# ─── P0 项目令牌/项目切换 ──────────────────────────────────────────────

def test_create_second_project_returns_its_own_token_and_result(c):
    a, h = create_blank(c, "企业A")
    old_id = a.json()["case"]["case_id"]
    old_token = h["X-Project-Token"]

    b = c.post("/api/cases/empty", headers=h, params={"name": "企业B"})
    assert b.status_code == 200, b.text[:220]
    bcase = b.json()["case"]
    assert bcase["case_id"] != old_id
    assert b.json()["project_token"] == bcase["access_token"] != old_token
    assert b.headers["X-Project-Token"] == bcase["access_token"]
    assert b.json()["results"]["case"]["case_id"] == bcase["case_id"]


def test_activate_other_owned_project_rebinds_request_token(c):
    a, h = create_blank(c, "企业A")
    aid = a.json()["case"]["case_id"]
    b = c.post("/api/cases/empty", headers=h, params={"name": "企业B"})
    bid = b.json()["case"]["case_id"]
    btoken = b.json()["case"]["access_token"]

    switched = c.post(f"/api/cases/{bid}/activate", headers=h)
    assert switched.status_code == 200
    assert switched.json()["case"]["case_id"] == bid
    assert switched.json()["project_token"] == btoken
    assert switched.json()["results"]["case"]["case_id"] == bid
    assert switched.headers["X-Project-Token"] == btoken

    next_h = {**h, "X-Project-Token": switched.headers["X-Project-Token"]}
    assert c.get("/api/case", headers=next_h).json()["case_id"] == bid
    assert aid != bid


def test_reset_demo_with_old_token_does_not_snap_back_to_enterprise(c):
    a, h = create_blank(c, "企业A")
    r = c.post("/api/reset-demo", headers=h)
    assert r.status_code == 200
    assert r.json()["results"]["case"]["case_id"] == store.JINHUI_ID
    # 协议必须显式告诉前端清除旧Token，或返回新的安全状态
    assert r.json().get("project_token") in ("", None)
    assert r.headers.get("X-Project-Token") in ("", None)
    # 后续GET不携带旧Token时，必须保持在公开案例
    no_token_headers = {"X-Session-Id": h["X-Session-Id"]}
    assert c.get("/api/case", headers=no_token_headers).json()["case_id"] == store.JINHUI_ID


# ─── P0 财务假设撤销 ──────────────────────────────────────────────────

def test_uncheck_temporary_zero_om_restores_missing_cost(c):
    _, h = create_blank(c)
    add_powerdrop_project(c, h, om=None)
    original = c.get("/api/results", headers=h).json()["selected_assessment"]
    assert original["simple_payback_years"] is None

    yes = c.put("/api/scenario", headers=h, json={"assume_zero_maintenance": True})
    assert yes.status_code == 200
    assert c.get("/api/results", headers=h).json()["selected_assessment"]["simple_payback_years"] is not None

    no = c.put("/api/scenario", headers=h, json={"assume_zero_maintenance": False})
    assert no.status_code == 200
    case = c.get("/api/case", headers=h).json()
    sa = c.get("/api/results", headers=h).json()["selected_assessment"]
    assert case["scenario"]["assume_zero_maintenance"] is False
    assert case["retrofits"][0]["annual_maintenance_cny"] is None
    assert sa["annual_maintenance_cny"] is None
    assert sa["simple_payback_years"] is None


def test_cleared_maintenance_field_must_not_remain_old_value(c):
    """网页字段说明‘清空=未知’；清空必须有与清空投资相同的API语义。"""
    _, h = create_blank(c)
    add_powerdrop_project(c, h, om=12000)
    assert c.get("/api/results", headers=h).json()["selected_assessment"]["annual_maintenance_cny"] == 12000
    # 当前端改为传明确清空标志时，后端必须处理，不可默默丢弃此字段。
    r = c.put("/api/scenario", headers=h, json={"clear_annual_maintenance_cny": True})
    assert r.status_code == 200, r.text[:220]
    sa = c.get("/api/results", headers=h).json()["selected_assessment"]
    assert sa["annual_maintenance_cny"] is None
    assert sa["simple_payback_years"] is None


# ─── P0 无设备绑定与物理能量边界 ───────────────────────────────────────

def test_unbound_imported_retrofit_is_draft_not_implementable(c):
    _, h = create_blank(c)
    raw = (
        "项目名称,目标设备,功率降低_kW,投资_元,年维护费_元\n"
        "未绑定技改,虚构设备,20,100000,0\n"
    ).encode("utf-8-sig")
    up = c.post(
        "/api/upload/commit?kind=retrofit", headers=h,
        files={"file": ("retrofits.csv", raw, "text/csv")}
    )
    # 可以拒绝导入，或接受为草稿；但绝不能推荐没有设备绑定的措施。
    if up.status_code in (400, 422):
        return
    assert up.status_code == 200, up.text[:220]
    t = c.put("/api/scenario", headers=h, json={"hours": 4000, "energy_price": 0.8})
    assert t.status_code == 200
    sa = c.get("/api/results", headers=h).json()["selected_assessment"]
    assert sa.get("recommendable") is False, "没有设备ID仍不得输出可实施/可推荐"
    assert sa.get("recommendation_blocked") is True


def test_power_drop_savings_must_not_exceed_same_device_annual_baseline(c):
    _, h = create_blank(c)
    add_powerdrop_project(c, h, baseline_kwh=10000, om=0)
    sa = c.get("/api/results", headers=h).json()["selected_assessment"]
    assert sa["saved_kwh"] == pytest.approx(80000)
    # 允许展示公式情景，但必须标示矛盾，阻断可靠经济推荐。
    assert sa.get("recommendable") is False
    assert sa.get("recommendation_blocked") is True
    assert sa.get("consistency_warnings")


# ─── P1 报价敏感性与未知维护费 ─────────────────────────────────────────

def test_quote_sensitivity_does_not_silently_assume_unknown_om_zero(c):
    _, h = create_blank(c)
    did, mid = add_powerdrop_project(c, h, om=None)
    uq = c.post("/api/procurement/user-quotes", headers=h, json={
        "label": "供应商A", "total_quote_cny": 100000, "device_type": "球磨机",
    })
    assert uq.status_code == 200
    qid = uq.json()["quote"]["quote_id"]
    apply = c.post("/api/procurement/apply-quote", headers=h, json={
        "quote_id": qid, "measure_id": mid, "as_price_sensitivity": True,
        "confirm_spec_and_scope": True,
    })
    assert apply.status_code == 200, apply.text[:220]
    data = c.get("/api/results", headers=h).json()
    assert data["selected_assessment"]["annual_maintenance_cny"] is None
    sens = data["assessment_snapshot"].get("price_sensitivity") or {}
    # 可显式试算零费用，但须独立、明确的假设确认；本测试没有确认。
    assert sens.get("hypothetical_payback_years") is None, sens


# ─── P1 报告指纹与示范来源 ───────────────────────────────────────────

def test_snapshot_fingerprint_changes_with_device_name_in_report(c):
    r = c.put("/api/scenario", json={"hours": 4000})
    assert r.status_code == 200
    old = c.get("/api/results").json()
    old_hash = old["assessment_snapshot"]["snapshot_id"]
    edit = c.put("/api/devices/dev-mill", json={"name": "已改名的选厂球磨机"})
    assert edit.status_code == 200
    new = c.get("/api/results").json()
    assert old["selected_assessment"]["saved_kwh"] == new["selected_assessment"]["saved_kwh"]
    assert old["selected_assessment"]["device_name"] != new["selected_assessment"]["device_name"]
    assert old_hash != new["assessment_snapshot"]["snapshot_id"]


def test_fork_by_scenario_edit_keeps_public_case_origin(c):
    r = c.put("/api/scenario", json={"hours": 2000})
    assert r.status_code == 200
    case = r.json()["case"]
    assert case["is_demo"] is False
    assert case["origin_case_id"] == store.JINHUI_ID


def test_create_blank_name_in_json_body_is_respected(c):
    r = c.post("/api/cases/empty", json={"name": "真正要创建的企业名称"})
    assert r.status_code == 200
    assert r.json()["case"]["name"] == "真正要创建的企业名称"


# ─── P1 报告历史重启后持久化 ──────────────────────────────────────────

def test_exported_report_archives_survive_server_restart(c):
    a, h = create_blank(c)
    r = c.post("/api/reports/export", headers=h, json={"preset": "full", "format": "txt"})
    assert r.status_code == 200
    before = c.get("/api/reports/archives", headers=h).json()["reports"]
    assert len(before) == 1

    # 全新Python解释器：真正丢弃进程内存，重载同一个临时测试数据库。
    snippet = r"""
import json
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app, raise_server_exceptions=False)
c.cookies.set("ltzs_sid", __CK_SID__)
c.cookies.set("ltzs_stk", __CK_MAC__)
headers = {"X-Session-Id": __SID__, "X-Project-Token": __TOKEN__}
r = c.get("/api/reports/archives", headers=headers)
print(json.dumps({"status": r.status_code, "count": len(r.json()["reports"])}))
""".replace("__SID__", repr(h["X-Session-Id"])).replace(
        "__TOKEN__", repr(h["X-Project-Token"])
    ).replace("__CK_SID__", repr(c.cookies.get("ltzs_sid"))).replace(
        "__CK_MAC__", repr(c.cookies.get("ltzs_stk"))
    )
    p = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True, text=True, timeout=15, check=True
    )
    line = p.stdout.strip().splitlines()[-1]
    after = json.loads(line)
    assert after["status"] == 200
    assert after["count"] == 1, "服务器重启后报告历史不应悄然清空"


# ─── P0 数据库与内存的并发持久化顺序 ──────────────────────────────────

def test_two_successful_concurrent_saves_must_persist_latest_revision(monkeypatch, c):
    r, h = create_blank(c)
    case_id = r.json()["case"]["case_id"]
    first_rev = r.json()["case"]["revision"] + 1
    original_persist = store._persist_case
    hit_first = threading.Event()
    release_first = threading.Event()

    def delayed_persist(state):
        # 人工将第一份写盘拖后，测试事务正确性；修复后应是串行或可靠CAS。
        if state.case_id == case_id and state.revision == first_rev:
            hit_first.set()
            release_first.wait(timeout=6)
        return original_persist(state)

    monkeypatch.setattr(store, "_persist_case", delayed_persist)

    def save(price):
        with TestClient(app, raise_server_exceptions=False) as sub:
            return sub.put("/api/scenario", headers=h, json={"energy_price": price})

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(save, 1.0)
        assert hit_first.wait(4), "未能进入并发测试拦截点"
        f2 = pool.submit(save, 2.0)
        # 修复版如果将锁覆盖到落盘，第二个请求会排队；这里不能无期限等待。
        time.sleep(0.3)
        release_first.set()
        a = f1.result(timeout=10)
        b = f2.result(timeout=10)

    assert a.status_code in (200, 409), a.text[:200]
    assert b.status_code in (200, 409), b.text[:200]
    memory = c.get("/api/case", headers=h).json()
    with sqlite3.connect(str(store.DB_PATH)) as con:
        row = con.execute("SELECT payload FROM projects WHERE case_id=?", (case_id,)).fetchone()
    assert row, "项目未保存"
    persisted = json.loads(row[0])
    assert persisted["revision"] == memory["revision"], (
        f"SQLite修订号{persisted['revision']}落后于内存{memory['revision']}"
    )
    assert persisted["scenario"]["energy_price"] == memory["scenario"]["energy_price"]


# ─── 正向对照：避免只包含故意失败的用例 ───────────────────────────────

def test_public_demo_calculation_remains_800000_kwh(c):
    sa = c.get("/api/results").json()["selected_assessment"]
    assert sa["saved_kwh"] == pytest.approx(800000)
    assert sa["co2_reduction_t"] == pytest.approx(357.68)


def test_explicit_zero_maintenance_is_valid_user_data(c):
    _, h = create_blank(c)
    add_powerdrop_project(c, h, om=0)
    sa = c.get("/api/results", headers=h).json()["selected_assessment"]
    assert sa["annual_maintenance_cny"] == 0
    assert sa["simple_payback_years"] == pytest.approx(1.56)


def test_bad_json_type_still_rejected(c):
    r = c.post("/api/equipment/assessments/calculate", json={"method_code": {"unexpected": 123}})
    assert r.status_code in (400, 422)
