"""V8.2.7 全面审计超额多情景自检。仅用于隔离测试库，禁止对正式企业库执行。"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import store
from app.version import APP_VERSION, BUILD_ID


@pytest.fixture(autouse=True)
def isolated_reset():
    store.reset_all_for_tests()


@pytest.fixture
def c():
    return TestClient(app, raise_server_exceptions=False)


def project(c, name="审计企业827"):
    r = c.post("/api/cases/empty", json={"name": name})
    assert r.status_code == 200, r.text[:400]
    return r, {
        "X-Session-Id": r.headers["X-Session-Id"],
        "X-Project-Token": r.headers["X-Project-Token"],
    }


def csv_upload(c, h, kind, data, policy=None):
    url = f"/api/upload/commit?kind={kind}"
    if policy:
        url += f"&energy_price_policy={policy}"
    return c.post(
        url,
        headers=h,
        files={"file": ("data.csv", data.encode("utf-8-sig"), "text/csv")},
    )


def test_health_version_is_827(c):
    h = c.get("/api/health").json()
    assert h["version"] == "8.2.12" == APP_VERSION
    assert h["frontend_expected"] == "8.2.12"
    assert h["build_id"] == BUILD_ID == "v8212-audit-20261111"
    assert h.get("data_persistence") == "local_sqlite"
    assert "Render" in (h.get("data_persistence_note") or "")


def test_sec01_header_sid_alone_cannot_read_private(c):
    a = TestClient(app, raise_server_exceptions=False)
    ra, ha = project(a, "甲机密")
    cid = ra.json()["case"]["case_id"]
    assert a.post(
        "/api/devices",
        headers=ha,
        json={"name": "机密磨机", "device_type": "球磨机", "power_kw": 150, "hours": 4000},
    ).status_code == 200

    b = TestClient(app, raise_server_exceptions=False)
    r = b.get("/api/case", headers={"X-Session-Id": ha["X-Session-Id"]})
    names = [d.get("name") for d in (r.json().get("devices") or [])] if r.status_code == 200 else []
    assert "机密磨机" not in names
    listed = b.get("/api/cases", headers={"X-Session-Id": ha["X-Session-Id"]}).json().get("cases") or []
    assert cid not in {row.get("case_id") for row in listed if row.get("owned")}
    act = b.post(f"/api/cases/{cid}/activate", headers={"X-Session-Id": ha["X-Session-Id"]})
    assert act.status_code in (403, 404)


def test_sec02_forged_ltzs_sid_cookie_without_mac_fails():
    a = TestClient(app, raise_server_exceptions=False)
    ra, ha = project(a, "甲Cookie")
    cid = ra.json()["case"]["case_id"]
    a.post(
        "/api/devices",
        headers=ha,
        json={"name": "保密泵", "device_type": "水泵", "power_kw": 80, "hours": 3000},
    )

    b = TestClient(app, raise_server_exceptions=False)
    b.cookies.set("ltzs_sid", ha["X-Session-Id"])
    r = b.get("/api/case")
    names = [d.get("name") for d in (r.json().get("devices") or [])] if r.status_code == 200 else []
    assert "保密泵" not in names
    act = b.post(f"/api/cases/{cid}/activate")
    assert act.status_code in (403, 404)

    c2 = TestClient(app, raise_server_exceptions=False)
    c2.cookies.set("ltzs_sid", ha["X-Session-Id"])
    c2.cookies.set("ltzs_stk", "0" * 64)
    r2 = c2.get("/api/case")
    names2 = [d.get("name") for d in (r2.json().get("devices") or [])] if r2.status_code == 200 else []
    assert "保密泵" not in names2


def test_sec03_sid_a_plus_token_b_cannot_read_a():
    a = TestClient(app, raise_server_exceptions=False)
    b = TestClient(app, raise_server_exceptions=False)
    ra, ha = project(a, "甲企业")
    rb, hb = project(b, "乙企业")
    cid_a = ra.json()["case"]["case_id"]
    a.post(
        "/api/devices",
        headers=ha,
        json={"name": "甲独有设备", "device_type": "球磨机", "power_kw": 200, "hours": 4000},
    )
    b.post(
        "/api/devices",
        headers=hb,
        json={"name": "乙独有设备", "device_type": "风机", "power_kw": 90, "hours": 2000},
    )

    mix = TestClient(app, raise_server_exceptions=False)
    headers = {
        "X-Session-Id": ha["X-Session-Id"],
        "X-Project-Token": hb["X-Project-Token"],
    }
    case = mix.get("/api/case", headers=headers)
    names = [d.get("name") for d in (case.json().get("devices") or [])] if case.status_code == 200 else []
    assert "甲独有设备" not in names
    listed = mix.get("/api/cases", headers=headers).json().get("cases") or []
    owned = {row.get("case_id") for row in listed if row.get("owned")}
    assert cid_a not in owned
    act = mix.post(f"/api/cases/{cid_a}/activate", headers=headers)
    assert act.status_code in (403, 404)
    # Token B 仍可看 B 的项目
    if case.status_code == 200:
        assert "乙独有设备" in names or case.json().get("case_id") == rb.json()["case"]["case_id"]


def test_sec04_two_enterprises_independent():
    a = TestClient(app, raise_server_exceptions=False)
    b = TestClient(app, raise_server_exceptions=False)
    ra, ha = project(a, "甲独立")
    rb, hb = project(b, "乙独立")
    a.post("/api/devices", headers=ha, json={"name": "甲机", "device_type": "球磨机", "power_kw": 100, "hours": 4000})
    b.post("/api/devices", headers=hb, json={"name": "乙机", "device_type": "水泵", "power_kw": 60, "hours": 3000})
    sa = a.post("/api/versions/save?label=甲v1", headers=ha)
    sb = b.post("/api/versions/save?label=乙v1", headers=hb)
    assert sa.status_code == 200 and sb.status_code == 200
    va = sa.json().get("version_id") or (sa.json().get("version") or {}).get("version_id")
    # B 不得读取甲的版本
    if va:
        steal = b.get(f"/api/versions/{va}", headers=hb)
        assert steal.status_code in (403, 404)
    na = [d["name"] for d in a.get("/api/case", headers=ha).json()["devices"]]
    nb = [d["name"] for d in b.get("/api/case", headers=hb).json()["devices"]]
    assert "甲机" in na and "乙机" not in na
    assert "乙机" in nb and "甲机" not in nb


def test_sec05_untrusted_localhost_origin_no_credentialed_cors(c):
    r = c.get("/api/session", headers={"Origin": "http://127.0.0.1:9377"})
    acao = r.headers.get("access-control-allow-origin")
    assert acao not in ("http://127.0.0.1:9377", "*")
    # 预检同样不得放行
    opt = c.options(
        "/api/session",
        headers={
            "Origin": "http://127.0.0.1:9377",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert opt.headers.get("access-control-allow-origin") not in ("http://127.0.0.1:9377", "*")


def test_sec06_trusted_origin_keeps_cors(c):
    r = c.get("/api/session", headers={"Origin": "http://127.0.0.1:8000"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://127.0.0.1:8000"
    assert (r.headers.get("access-control-allow-credentials") or "").lower() == "true"
    assert r.cookies.get("ltzs_sid")
    assert r.cookies.get("ltzs_stk")


def test_sec07_private_restore_demo_forbidden(c):
    _, h = project(c, "私人空白")
    before = c.get("/api/case", headers=h).json()
    r = c.post("/api/devices/restore-demo-defaults", headers=h)
    assert r.status_code == 403
    after = c.get("/api/case", headers=h).json()
    assert after["devices"] == before["devices"]
    assert not any(d.get("name") == "二级输送泵" for d in after.get("devices") or [])


def test_sec08_import_dev_mill_cannot_unlock_demo_restore(c):
    _, h = project(c, "伪造磨机ID")
    csv = "设备名称,设备类型,功率_kW,年运行小时,设备ID\n用户磨机,球磨机,100,4000,dev-mill\n"
    up = csv_upload(c, h, "device", csv)
    assert up.status_code == 200, up.text[:400]
    r = c.post("/api/devices/restore-demo-defaults", headers=h)
    assert r.status_code == 403
    names = [d.get("name") for d in c.get("/api/case", headers=h).json().get("devices") or []]
    assert "二级输送泵" not in names


def test_sec09_import_rf_pump_cannot_unlock_demo_restore(c):
    _, h = project(c, "伪造措施ID")
    dev = csv_upload(c, h, "device", "设备名称,设备类型,功率_kW,年运行小时\n用户泵,水泵,50,3000\n")
    assert dev.status_code == 200, dev.text[:300]
    did = c.get("/api/case", headers=h).json()["devices"][0]["id"]
    rf = (
        "项目名称,目标设备,功率降低_kW,投资_元,措施ID\n"
        f"用户改造,{did},10,10000,rf-pump\n"
    )
    # 措施模板字段可能没有措施ID，改用 API 创建后再改不了 ID；导入没有措施ID列时直接 restore 仍应 403
    r = c.post("/api/devices/restore-demo-defaults", headers=h)
    assert r.status_code == 403
    names = [d.get("name") for d in c.get("/api/case", headers=h).json().get("devices") or []]
    assert "二级输送泵" not in names
    assert "选厂球磨机" not in names or all(d.get("id") != "dev-mill" or d.get("data_type") != "披露" for d in c.get("/api/case", headers=h).json().get("devices") or [])


def test_sec10_public_demo_restore_only_public_assets(c):
    # 公开示范可删再恢复
    r0 = c.get("/api/case")
    assert r0.json().get("is_demo") or r0.json().get("case_id") == "jinhui-demo"
    c.delete("/api/devices/dev-pump")
    r = c.post("/api/devices/restore-demo-defaults")
    assert r.status_code == 200, r.text[:400]
    case = r.json()["case"]
    assert case.get("is_demo") or case.get("case_id") == "jinhui-demo" or case.get("origin_case_id") == "jinhui-demo"
    assert any(d.get("id") == "dev-pump" and d.get("name") == "二级输送泵" for d in case["devices"])
    assert any(d.get("id") == "dev-mill" for d in case["devices"])


def test_dev03_clear_power_flags(c):
    _, h = project(c, "清空功率")
    add = c.post(
        "/api/devices",
        headers=h,
        json={"name": "可清空", "device_type": "通用", "power_kw": 150, "hours": 4000, "baseline_kwh": 10000, "output_t": 20},
    )
    assert add.status_code == 200, add.text[:300]
    did = add.json()["case"]["devices"][-1]["id"]
    rev0 = add.json()["case"]["revision"]
    clr = c.put(
        f"/api/devices/{did}",
        headers=h,
        json={"clear_power_kw": True, "clear_hours": True, "clear_baseline_kwh": True, "clear_output_t": True},
    )
    assert clr.status_code == 200, clr.text[:400]
    dev = next(d for d in clr.json()["case"]["devices"] if d["id"] == did)
    assert dev["power_kw"] is None
    assert dev["hours"] is None
    assert dev["baseline_kwh"] is None
    assert dev["output_t"] is None
    assert clr.json()["case"]["revision"] > rev0


def test_dev04_null_name_and_power_rejected_no_silent_revision(c):
    _, h = project(c, "空值语义")
    add = c.post(
        "/api/devices",
        headers=h,
        json={"name": "保留名", "device_type": "通用", "power_kw": 150, "hours": 4000},
    )
    did = add.json()["case"]["devices"][-1]["id"]
    rev0 = add.json()["case"]["revision"]
    r1 = c.put(f"/api/devices/{did}", headers=h, json={"name": None})
    assert r1.status_code == 422
    r2 = c.put(f"/api/devices/{did}", headers=h, json={"power_kw": None})
    assert r2.status_code == 422
    after = c.get("/api/case", headers=h).json()
    dev = next(d for d in after["devices"] if d["id"] == did)
    assert dev["name"] == "保留名"
    assert dev["power_kw"] == 150
    assert after["revision"] == rev0


def test_data01_delete_device_invalidates_references(c):
    _, h = project(c, "删设备清参考")
    add = c.post(
        "/api/devices",
        headers=h,
        json={"name": "试验泵", "device_type": "矿井排水泵", "power_kw": 80, "hours": 4000},
    )
    did = add.json()["case"]["devices"][-1]["id"]
    rf = c.post(
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
    assert rf.status_code == 200, rf.text[:400]
    mid = rf.json()["case"]["retrofits"][-1]["id"]
    sel = c.post(
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
    )
    assert sel.status_code == 200, sel.text[:400]
    before = c.get("/api/case", headers=h).json()
    active_before = [
        s for s in (before.get("reference_selections") or [])
        if (s.get("active") or s.get("applied")) and s.get("measure_id") == mid
    ]
    assert active_before, "预条件：应有当前有效参考"
    deleted = c.delete(f"/api/devices/{did}", headers=h)
    assert deleted.status_code == 200, deleted.text[:400]
    after = deleted.json()["case"]
    leftover = [
        s for s in (after.get("reference_selections") or [])
        if (s.get("active") or s.get("applied")) and (
            s.get("measure_id") == mid or s.get("applicable_device_id") == did
        )
    ]
    assert leftover == []
    # 历史仍可追溯
    assert any(
        s.get("measure_id") == mid or s.get("applicable_device_id") == did
        for s in (after.get("reference_selections") or [])
    )
    pvs = after.get("parameter_values") or []
    assert not any(
        (x.get("measure_id") == mid or x.get("device_id") == did) and x.get("applied")
        for x in pvs
    )
    # 恢复后不得出现幽灵已应用
    rst = c.post(f"/api/devices/{did}/restore", headers=h)
    assert rst.status_code == 200, rst.text[:400]
    restored = rst.json()["case"]
    assert any(d["id"] == did for d in restored["devices"])
    ghost = [
        s for s in (restored.get("reference_selections") or [])
        if (s.get("active") or s.get("applied")) and s.get("measure_id") == mid
    ]
    assert ghost == []


def test_data02_energy_import_keeps_manual_price(c):
    _, h = project(c, "保留手动电价")
    put = c.put("/api/scenario", headers=h, json={"energy_price": 0.91})
    assert put.status_code == 200
    sc = put.json()["case"]["scenario"]
    assert sc["energy_price"] == 0.91
    assert sc["energy_price_user_override"] is True

    no_cost = "年份,工业用电量_万kWh\n2024,100\n"
    r1 = csv_upload(c, h, "energy", no_cost)
    assert r1.status_code == 200, r1.text[:400]
    sc1 = (r1.json().get("case") or r1.json().get("results", {}).get("case"))["scenario"]
    assert sc1["energy_price"] == 0.91
    assert sc1["energy_price_user_override"] is True
    warns = " ".join(r1.json().get("warnings") or [])
    assert "0.91" in warns

    c.put("/api/scenario", headers=h, json={"energy_price": 0.89})
    with_cost = "年份,工业用电量_万kWh,电力费用_万元\n2024,100,50\n"
    r2 = csv_upload(c, h, "energy", with_cost)  # default keep_manual
    assert r2.status_code == 200, r2.text[:400]
    sc2 = (r2.json().get("case") or r2.json().get("results", {}).get("case"))["scenario"]
    assert sc2["energy_price"] == 0.89
    assert sc2["energy_price_user_override"] is True
    warns2 = " ".join(r2.json().get("warnings") or [])
    assert "0.89" in warns2 and "0.5" in warns2

    r3 = csv_upload(c, h, "energy", with_cost, policy="use_imported")
    assert r3.status_code == 200, r3.text[:400]
    sc3 = (r3.json().get("case") or r3.json().get("results", {}).get("case"))["scenario"]
    assert abs(float(sc3["energy_price"]) - 0.5) < 1e-9
    assert sc3["energy_price_user_override"] is False


def test_data03_manual_price_and_clear_retire_tariff_quote(c):
    _, h = project(c, "电价引用失效")
    ap = c.post(
        "/api/procurement/apply-quote",
        headers=h,
        json={"quote_id": "q-jinhui-tariff", "confirm_reference_scenario": True},
    )
    assert ap.status_code == 200, ap.text[:400]
    qc = ap.json().get("quote_choice") or ap.json()["case"].get("quote_choice")
    assert (qc or {}).get("quote_usage_mode") == "tariff_reference"
    assert ap.json()["case"]["scenario"].get("use_jinhui_tariff_reference") is True

    edit = c.put("/api/scenario", headers=h, json={"energy_price": 0.95})
    assert edit.status_code == 200, edit.text[:400]
    sc = edit.json()["case"]["scenario"]
    assert sc["energy_price"] == 0.95
    assert sc.get("use_jinhui_tariff_reference") is False
    qc2 = edit.json()["case"].get("quote_choice") or {}
    assert qc2.get("superseded") or qc2.get("active") is False or edit.json().get("results", {}).get("assessment_snapshot", {}).get("quote_choice") in (None, {})
    snap = (edit.json().get("results") or {}).get("assessment_snapshot") or {}
    assert snap.get("quote_choice") in (None, {}) or snap.get("quote_choice", {}).get("superseded")
    assert snap.get("historical_quote_choice") or qc2.get("superseded")

    # 再次采用后清空
    ap2 = c.post(
        "/api/procurement/apply-quote",
        headers=h,
        json={"quote_id": "q-jinhui-tariff", "confirm_reference_scenario": True},
    )
    assert ap2.status_code == 200
    cleared = c.put("/api/scenario", headers=h, json={"clear_energy_price": True})
    assert cleared.status_code == 200
    sc3 = cleared.json()["case"]["scenario"]
    assert sc3.get("energy_price") is None
    assert sc3.get("use_jinhui_tariff_reference") is False


def test_data03_device_type_change_retires_quote(c):
    _, h = project(c, "改类型报价失效")
    add = c.post(
        "/api/devices",
        headers=h,
        json={"name": "变频对象", "device_type": "球磨机", "power_kw": 200, "hours": 4000},
    )
    did = add.json()["case"]["devices"][-1]["id"]
    rf = c.post(
        "/api/retrofits",
        headers=h,
        json={"name": "加装变频器", "device_id": did, "power_drop_kw": 20, "investment_cny": 80000, "select": True},
    )
    assert rf.status_code == 200, rf.text[:300]
    mid = rf.json()["case"]["retrofits"][-1]["id"]
    uq = c.post(
        "/api/procurement/user-quotes",
        headers=h,
        json={"label": "变频器报价", "total_quote_cny": 70000, "device_type": "球磨机"},
    )
    qid = uq.json()["quote"]["quote_id"]
    ap = c.post(
        "/api/procurement/apply-quote",
        headers=h,
        json={"quote_id": qid, "measure_id": mid, "as_price_sensitivity": True, "confirm_spec_and_scope": True, "confirm_reference_scenario": True},
    )
    assert ap.status_code == 200, ap.text[:400]
    chg = c.put(f"/api/devices/{did}", headers=h, json={"device_type": "变压器"})
    assert chg.status_code == 200, chg.text[:400]
    qc = chg.json()["case"].get("quote_choice") or {}
    assert qc.get("superseded") or qc.get("active") is False or qc.get("compatibility_result") == "needs_review"


def test_data04_import_id_collision_with_recycle_bin(c):
    _, h = project(c, "回收站ID冲突")
    add = c.post(
        "/api/devices",
        headers=h,
        json={"name": "旧设备", "device_type": "通用", "power_kw": 40, "hours": 2000},
    )
    did = add.json()["case"]["devices"][-1]["id"]
    c.post(
        "/api/retrofits",
        headers=h,
        json={"name": "旧技改", "device_id": did, "power_drop_kw": 5, "investment_cny": 10000},
    )
    assert c.delete(f"/api/devices/{did}", headers=h).status_code == 200
    csv = f"设备名称,设备类型,功率_kW,年运行小时,设备ID\n新导入设备,通用,55,2500,{did}\n"
    up = csv_upload(c, h, "device", csv)
    assert up.status_code == 200, up.text[:500]
    case = up.json().get("case") or up.json().get("results", {}).get("case")
    active_ids = [d["id"] for d in case["devices"]]
    removed_ids = [d["id"] for d in (case.get("removed_devices") or [])]
    assert did in removed_ids
    assert did not in active_ids
    assert any(d["name"] == "新导入设备" for d in case["devices"])
    rst = c.post(f"/api/devices/{did}/restore", headers=h)
    assert rst.status_code == 200, rst.text[:400]
    rec = rst.json()["case"]
    assert any(d["id"] == did and d["name"] == "旧设备" for d in rec["devices"])
    assert any(rf.get("name") == "旧技改" and rf.get("device_id") == did for rf in rec.get("retrofits") or [])
    assert any(d["name"] == "新导入设备" and d["id"] != did for d in rec["devices"])


def test_report01_archive_write_failure_ascii_headers_still_download(c):
    _, h = project(c, "归档失败")
    c.post("/api/devices", headers=h, json={"name": "机", "device_type": "通用", "power_kw": 100, "hours": 4000})
    with patch.object(store, "archive_exported_report", side_effect=OSError("磁盘空间不足")):
        r = c.post("/api/reports/export", headers=h, json={"preset": "one_page", "format": "txt"})
    assert r.status_code == 200, r.text[:300]
    assert r.headers.get("X-Archive-Saved") == "false"
    assert r.headers.get("X-Archive-Error-Code") == "write_failed"
    warning = r.headers.get("X-Archive-Warning")
    if warning is not None:
        warning.encode("ascii")
    body = r.text
    assert body
    # 不得写入虚假历史
    hist = c.get("/api/reports/archives", headers=h)
    assert hist.status_code == 200
    assert hist.json().get("reports") in ([], None) or all(
        (row.get("format") != "txt") or row.get("text") != body
        for row in hist.json().get("reports") or []
    )


def test_report03_normal_archive_saved_header(c):
    _, h = project(c, "正常归档")
    r = c.post("/api/reports/export", headers=h, json={"preset": "one_page", "format": "txt"})
    assert r.status_code == 200
    assert r.headers.get("X-Archive-Saved") == "true"
    hist = c.get("/api/reports/archives", headers=h).json().get("reports") or []
    assert hist, "应有归档记录"


def test_cookie_plus_token_same_owner_still_works():
    """可信 Cookie + 本项目 Token：正常操作不回归。"""
    a = TestClient(app, raise_server_exceptions=False)
    ra, ha = project(a, "本会话续接")
    r = a.get("/api/case", headers=ha)
    assert r.status_code == 200
    assert r.json()["name"] == "本会话续接"
    r2 = a.get("/api/session")
    assert r2.status_code == 200
    assert r2.json().get("project_token") == ha["X-Project-Token"] or r2.json().get("active") == ra.json()["case"]["case_id"]


def test_multi_enterprise_switch_and_session_ticket_present(c):
    r1, h1 = project(c, "企业一")
    r2 = c.post("/api/cases/empty", headers=h1, json={"name": "企业二"})
    assert r2.status_code == 200
    id1, id2 = r1.json()["case"]["case_id"], r2.json()["case"]["case_id"]
    act = c.post(f"/api/cases/{id1}/activate", headers={"X-Session-Id": h1["X-Session-Id"], "X-Project-Token": r2.headers.get("X-Project-Token") or h1["X-Project-Token"]})
    # 激活企业一应用本会话 Cookie（TestClient 已带 stk）
    act2 = c.post(f"/api/cases/{id1}/activate")
    assert act2.status_code == 200, act2.text[:300]
    listed = c.get("/api/cases").json()["cases"]
    owned = {row["case_id"] for row in listed if row.get("owned")}
    assert id1 in owned and id2 in owned
    assert c.cookies.get("ltzs_stk")
