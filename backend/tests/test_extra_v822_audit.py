# 独立封版补测：请只在隔离、空数据库的项目克隆上运行。
# WARNING: store.reset_all_for_tests() 会清空测试数据库，严禁用于正式企业库。
# 推荐放入 backend/tests/test_extra_v822_audit.py 后执行：
# python -m pytest -q tests/test_extra_v822_audit.py
from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import store


@pytest.fixture(autouse=True)
def reset_store():
    store.reset_all_for_tests()


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def csvfile(headers, rows):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8-sig")


def upload(client, kind, headers, rows):
    data = csvfile(headers, rows)
    return client.post(
        f"/api/upload/commit?kind={kind}",
        files={"file": (f"{kind}.csv", data, "text/csv")},
    )


def pump_case(client):
    assert client.put("/api/scenario", json={"hours": 4000}).status_code == 200
    dev = client.post(
        "/api/devices",
        json={
            "name": "Pump A",
            "device_type": "循环水泵",
            "power_kw": 55,
            "hours": 4000,
            "baseline_kwh": 200000,
        },
    )
    assert dev.status_code == 200, dev.text
    did = dev.json()["case"]["devices"][-1]["id"]
    measure = client.post(
        "/api/retrofits",
        json={
            "name": "Pump Retrofit",
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
        },
    )
    assert measure.status_code == 200, measure.text
    mid = measure.json()["case"]["retrofits"][-1]["id"]
    return did, mid


def test_missing_maintenance_must_remain_unknown(client):
    r1 = upload(
        client, "device",
        ["设备名称", "设备类型", "功率_kW", "年运行小时"],
        [["磨机A", "球磨机", 100, 4000]],
    )
    assert r1.status_code == 200 and r1.json()["accepted"], r1.text
    assert client.put(
        "/api/scenario", json={"hours": 4000, "energy_price": 0.8}
    ).status_code == 200
    r2 = upload(
        client, "retrofit",
        ["项目名称", "目标设备", "功率降低_kW", "投资_元"],
        [["降功率", "磨机A", 20, 100000]],
    )
    assert r2.status_code == 200 and r2.json()["accepted"], r2.text
    rf = client.get("/api/case").json()["retrofits"][0]
    assert rf["annual_maintenance_cny"] is None, "缺失维护费不能被转成0"
    result = client.get("/api/results").json()["selected_assessment"]
    assert result["saved_kwh"] == pytest.approx(80000)
    assert result.get("simple_payback_years") is None, "缺维护费不能给确定回本期"


def test_demo_power_change_is_reflected_in_selected_measure_and_result(client):
    r = client.put(
        "/api/scenario",
        json={
            "hours": 4000,
            "power_drop_kw": 150,
            "selected_measure_id": "rf-mill",
        },
    )
    assert r.status_code == 200, r.text
    case = client.get("/api/case").json()
    selected = next(r for r in case["retrofits"] if r["id"] == "rf-mill")
    assert selected["power_drop_kw"] == pytest.approx(150)
    result = client.get("/api/results").json()["selected_assessment"]
    assert result["saved_kwh"] == pytest.approx(600000)


def test_device_replacement_must_invalidate_orphan_bindings(client):
    did, mid = pump_case(client)
    r = client.post(
        "/api/references/select",
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
    )
    assert r.status_code == 200 and r.json().get("applied"), r.text
    r = upload(
        client, "device",
        ["设备名称", "设备类型", "功率_kW", "年运行小时"],
        [["Pump B", "循环水泵", 60, 4000]],
    )
    assert r.status_code == 200 and r.json().get("accepted"), r.text
    case = client.get("/api/case").json()
    mids = {m["id"] for m in case["retrofits"]}
    dids = {d["id"] for d in case["devices"]}
    for binding in case["reference_selections"]:
        if binding.get("active") or binding.get("applied"):
            assert binding.get("measure_id") in mids
            assert binding.get("applicable_device_id") in dids


def test_two_immediate_version_saves_both_work(client):
    assert client.put("/api/scenario", json={"hours": 4000}).status_code == 200
    a = client.post("/api/versions/save", params={"label": "第一版"})
    b = client.post("/api/versions/save", params={"label": "第二版"})
    assert a.status_code == 200, a.text
    assert b.status_code == 200, b.text
    assert a.json()["version_id"] != b.json()["version_id"]


def test_use_own_with_wrong_unit_must_be_rejected(client):
    did, mid = pump_case(client)
    old = next(
        x for x in client.get("/api/case").json()["retrofits"]
        if x["id"] == mid
    )["model_inputs"]["q_m3_h"]
    r = client.post(
        "/api/references/select",
        json={
            "source_id": "gb-19762-2025",
            "applicable_device_id": did,
            "method_code": "pump_hydraulic",
            "parameter_key": "q_m3_h",
            "unit": "m3/s",  # 错误：q_m3_h 要求 m3/h
            "choice": "use_own",
            "parameter_value": 200,
            "measure_id": mid,
        },
    )
    assert r.status_code in (400, 422), r.text
    new = next(
        x for x in client.get("/api/case").json()["retrofits"]
        if x["id"] == mid
    )["model_inputs"]["q_m3_h"]
    assert new == old, "错误单位的请求不得污染旧数值"
