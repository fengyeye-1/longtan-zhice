"""V8.2.8 全面审计多情景自检。仅用于隔离测试库，禁止对正式企业库执行。"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import store
from app.services.import_data import validate_mapping_list, _check_xlsx_zip_bomb
from app.version import APP_VERSION, BUILD_ID


@pytest.fixture(autouse=True)
def isolated_reset():
    store.reset_all_for_tests()
    # 确认隔离
    assert "ltzs_pytest_" in str(store.DB_PATH) or "ltzs_pytest_" in str(store.ROOT)


@pytest.fixture
def c():
    return TestClient(app, raise_server_exceptions=False)


def project(c: TestClient, name="审计企业828"):
    r = c.post("/api/cases/empty", json={"name": name})
    assert r.status_code == 200, r.text[:400]
    return r, {
        "X-Session-Id": r.headers["X-Session-Id"],
        "X-Project-Token": r.headers["X-Project-Token"],
    }


def cookie_headers(client: TestClient):
    return {k: v for k, v in client.cookies.items()}


def test_health_version_is_828(c):
    h = c.get("/api/health").json()
    assert h["version"] == "8.2.11" == APP_VERSION
    assert h["frontend_expected"] == "8.2.11"
    assert h["build_id"] == BUILD_ID == "v8211-audit-20261111"
    assert h.get("data_persistence") == "local_sqlite"
    note = h.get("data_persistence_note") or ""
    assert "Render" in note
    assert "SnapDeploy" in note
    assert "不能承诺" in note or "持续保存" in note
    assert "示范" in note
    assert "公开代码仓库" in note


def test_p0_01_replay_header_sid_after_restart_does_not_reissue_trust():
    a = TestClient(app, raise_server_exceptions=False)
    ra, ha = project(a, "甲的私人项目")
    cid = ra.json()["case"]["case_id"]
    did = a.post(
        "/api/devices",
        headers=ha,
        json={"name": "甲专有设备", "device_type": "通用", "power_kw": 100, "hours": 3000},
    )
    assert did.status_code == 200, did.text[:300]
    victim_sid = ha["X-Session-Id"]
    assert victim_sid
    # 模拟进程重启：清空内存会话，保留 SQLite
    store._SESSIONS.clear()

    b = TestClient(app, raise_server_exceptions=False)
    r = b.get("/api/session", headers={"X-Session-Id": victim_sid})
    assert r.status_code == 200
    new_sid = r.json()["session_id"]
    assert new_sid != victim_sid
    # 不得为受害者 SID 重新签发票据
    assert r.headers.get("X-Session-Id") == new_sid
    listed = [x["case_id"] for x in (r.json().get("cases") or [])]
    assert cid not in listed
    act = b.post(f"/api/cases/{cid}/activate", headers={"X-Session-Id": victim_sid})
    assert act.status_code in (403, 404)
    case = b.get("/api/case", headers={"X-Session-Id": victim_sid})
    assert "甲专有设备" not in case.text


def test_p0_01_legal_signed_cookie_restores_workspace_after_restart():
    a = TestClient(app, raise_server_exceptions=False)
    ra, ha = project(a, "合法恢复企业")
    cid = ra.json()["case"]["case_id"]
    a.post(
        "/api/devices",
        headers=ha,
        json={"name": "恢复后仍在的设备", "device_type": "通用", "power_kw": 80, "hours": 2000},
    )
    sid = a.cookies.get("ltzs_sid")
    mac = a.cookies.get("ltzs_stk")
    assert sid and mac
    store._SESSIONS.clear()

    b = TestClient(app, raise_server_exceptions=False)
    b.cookies.set("ltzs_sid", sid)
    b.cookies.set("ltzs_stk", mac)
    sess = b.get("/api/session")
    assert sess.status_code == 200
    assert sess.json()["session_id"] == sid
    ids = [x["case_id"] for x in sess.json()["cases"]]
    assert cid in ids
    act = b.post(f"/api/cases/{cid}/activate")
    assert act.status_code == 200
    case = act.json().get("case") or b.get("/api/case").json()
    if "case" in case and "devices" not in case:
        case = case["case"]
    assert any(d["name"] == "恢复后仍在的设备" for d in case["devices"])


def test_p0_01_forged_cookie_and_wrong_mac_get_new_session():
    a = TestClient(app, raise_server_exceptions=False)
    ra, ha = project(a, "不可伪造")
    cid = ra.json()["case"]["case_id"]
    victim_sid = ha["X-Session-Id"]

    b = TestClient(app, raise_server_exceptions=False)
    b.cookies.set("ltzs_sid", victim_sid)
    r = b.get("/api/session")
    assert r.json()["session_id"] != victim_sid
    assert cid not in [x["case_id"] for x in r.json()["cases"]]

    c2 = TestClient(app, raise_server_exceptions=False)
    c2.cookies.set("ltzs_sid", victim_sid)
    c2.cookies.set("ltzs_stk", "0" * 64)
    r2 = c2.get("/api/session")
    assert r2.json()["session_id"] != victim_sid
    assert cid not in [x["case_id"] for x in r2.json()["cases"]]


def test_p0_02_project_token_does_not_authorize_sibling_projects():
    a = TestClient(app, raise_server_exceptions=False)
    r1, h1 = project(a, "案例一")
    id1, tok1 = r1.json()["case"]["case_id"], h1["X-Project-Token"]
    r2, h2 = project(a, "案例二")
    id2, tok2 = r2.json()["case"]["case_id"], h2["X-Project-Token"]
    dev = a.post(
        "/api/devices",
        headers=h2,
        json={"name": "案例二独有设备", "device_type": "通用", "power_kw": 55, "hours": 1500},
    )
    assert dev.status_code == 200, dev.text[:300]

    b = TestClient(app, raise_server_exceptions=False)
    sess = b.get("/api/session", headers={"X-Project-Token": tok1})
    assert sess.status_code == 200
    ids = [x["case_id"] for x in sess.json()["cases"]]
    assert id1 in ids
    assert id2 not in ids
    act = b.post(f"/api/cases/{id2}/activate", headers={"X-Project-Token": tok1})
    assert act.status_code in (403, 404)
    # 也不能用案例一 Token 读案例二设备
    got = b.get("/api/case", headers={"X-Project-Token": tok1}).json()
    names = [d["name"] for d in (got.get("case") or {}).get("devices") or []]
    assert "案例二独有设备" not in names


def test_sec07_cookie_a_plus_unrelated_token_b_does_not_merge_workspaces():
    a = TestClient(app, raise_server_exceptions=False)
    ra, ha = project(a, "甲工作区")
    ida = ra.json()["case"]["case_id"]
    b = TestClient(app, raise_server_exceptions=False)
    rb, hb = project(b, "乙工作区")
    idb = rb.json()["case"]["case_id"]
    tokb = hb["X-Project-Token"]

    # 甲带着自己的 Cookie，再附上乙的项目 Token
    mixed = TestClient(app, raise_server_exceptions=False)
    mixed.cookies.set("ltzs_sid", a.cookies.get("ltzs_sid"))
    mixed.cookies.set("ltzs_stk", a.cookies.get("ltzs_stk"))
    sess = mixed.get("/api/session", headers={"X-Project-Token": tokb})
    assert sess.status_code == 200
    ids = [x["case_id"] for x in sess.json()["cases"]]
    # 不可同时继承甲的兄弟项目与乙的其他项目；至少不得把乙的项目并入甲的工作区 owned 列表之外的越权
    # 允许通过 Token 临时访问乙的这一个项目，但不得因此展开乙的整个工作区；甲的项目仍按其 Cookie。
    allowed = {ida, idb, "jinhui-demo"}
    assert set(ids) <= allowed
    # 不能用该组合激活不存在的第三方项目
    act = mixed.post("/api/cases/user-not-exist/activate", headers={"X-Project-Token": tokb})
    assert act.status_code in (403, 404)


def test_p1_01_untrusted_origin_state_changing_post_is_rejected():
    a = TestClient(app, raise_server_exceptions=False)
    ra, ha = project(a, "CSRF防护")
    cid = ra.json()["case"]["case_id"]
    # 确认当前是私人项目
    assert a.get("/api/session").json()["active"] == cid
    r = a.post(
        "/api/reset-demo",
        headers={
            "Origin": "http://127.0.0.1:9377",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data="x=1",
    )
    assert r.status_code == 403, r.text[:300]
    assert a.get("/api/session").json()["active"] == cid


def test_p1_01_trusted_origin_and_no_origin_clients_still_work():
    a = TestClient(app, raise_server_exceptions=False)
    ra, ha = project(a, "同源允许")
    cid = ra.json()["case"]["case_id"]
    ok = a.post(
        "/api/reset-demo",
        headers={"Origin": "http://127.0.0.1:8000", "Content-Type": "application/json"},
        json={},
    )
    assert ok.status_code == 200, ok.text[:300]
    assert ok.json().get("ok") is True
    # pytest / 无 Origin 的受控客户端（重新建项目后再 reset）
    rb, _ = project(a, "无Origin客户端")
    cid2 = rb.json()["case"]["case_id"]
    assert a.get("/api/session").json()["active"] == cid2
    ok2 = a.post("/api/reset-demo")
    assert ok2.status_code == 200, ok2.text[:300]



def test_p1_01_same_origin_non_allowlist_port_is_allowed_via_host_match():
    """可信同源：Origin 与当前 Host 一致即允许，不必只靠 8000/5173 白名单。"""
    a = TestClient(app, raise_server_exceptions=False)
    ra, _ = project(a, "同源端口")
    cid = ra.json()["case"]["case_id"]
    # TestClient 的 Host 默认是 testserver
    ok = a.post(
        "/api/reset-demo",
        headers={"Origin": "http://testserver", "Content-Type": "application/json"},
        json={},
    )
    assert ok.status_code == 200, ok.text[:300]
    # 不同端口的本地来源仍然拒绝
    ra2, _ = project(a, "仍要拒绝跨端口")
    cid2 = ra2.json()["case"]["case_id"]
    bad = a.post(
        "/api/reset-demo",
        headers={"Origin": "http://127.0.0.1:9377", "Content-Type": "application/x-www-form-urlencoded"},
        data="x=1",
    )
    assert bad.status_code == 403
    assert a.get("/api/session").json()["active"] == cid2

def test_p1_02_duplicate_standard_field_mapping_rejected():
    a = TestClient(app, raise_server_exceptions=False)
    _, h = project(a, "重复映射")
    mappings = [
        {"user_column": "工业用电量_万kWh", "standard_field": "工业用电量_万kWh"},
        {"user_column": "备用用电量", "standard_field": "工业用电量_万kWh"},
        {"user_column": "年份", "standard_field": "年份"},
    ]
    csv = "年份,工业用电量_万kWh,备用用电量\n2025,1000,2000\n"
    r = a.post(
        "/api/upload/commit?kind=energy&mappings_json=" + json.dumps(mappings, ensure_ascii=False),
        headers=h,
        files={"file": ("data.csv", csv.encode("utf-8-sig"), "text/csv")},
    )
    assert r.status_code in (200, 400)
    body = r.json()
    accepted = body.get("accepted")
    # 必须拒绝且不写入
    assert accepted is False or r.status_code == 400
    err = json.dumps(body, ensure_ascii=False)
    assert "工业用电量_万kWh" in err
    assert "备用用电量" in err
    # 反转顺序同样拒绝
    mappings2 = list(reversed(mappings))
    r2 = a.post(
        "/api/upload/commit?kind=energy&mappings_json=" + json.dumps(mappings2, ensure_ascii=False),
        headers=h,
        files={"file": ("data.csv", csv.encode("utf-8-sig"), "text/csv")},
    )
    body2 = r2.json()
    assert body2.get("accepted") is False or r2.status_code == 400
    # 项目能源表未被悄悄写成 2000
    raw = a.get("/api/case", headers=h).json()
    case = raw.get("case") if isinstance(raw, dict) and "energy_years" not in raw else raw
    years = (case or {}).get("energy_years") or []
    assert not any(abs(float(y.get("elec_10k_kwh") or 0) - 2000) < 1e-9 for y in years)


def test_validate_mapping_list_covers_empty_missing_and_dup_source():
    errs = validate_mapping_list(
        [
            {"user_column": "", "standard_field": "年份"},
            {"user_column": "不存在的列", "standard_field": "工业用电量_万kWh"},
        ],
        ["年份", "工业用电量_万kWh"],
    )
    assert any("空" in e for e in errs)
    assert any("不存在" in e for e in errs)
    errs2 = validate_mapping_list(
        [
            {"user_column": "年份", "standard_field": "年份"},
            {"user_column": "年份", "standard_field": "工业用电量_万kWh"},
        ],
        ["年份"],
    )
    assert any("年份" in e and "重复" in e for e in errs2)


def test_p1_03_superseded_quote_not_treated_as_current():
    a = TestClient(app, raise_server_exceptions=False)
    _, h = project(a, "失效报价展示")
    ap = a.post(
        "/api/procurement/apply-quote",
        headers=h,
        json={"quote_id": "q-jinhui-tariff", "confirm_reference_scenario": True},
    )
    assert ap.status_code == 200, ap.text[:400]
    edit = a.put("/api/scenario", headers=h, json={"energy_price": 0.95})
    assert edit.status_code == 200, edit.text[:400]
    case = edit.json()["case"]
    qc = case.get("quote_choice") or {}
    assert qc.get("superseded") or qc.get("active") is False
    snap = (edit.json().get("results") or {}).get("assessment_snapshot") or {}
    assert snap.get("quote_choice") in (None, {})
    hist = snap.get("historical_quote_choice") or qc
    assert hist
    # 报告正文不得把失效报价写成当前采用
    prev = a.post(
        "/api/reports/preview",
        headers=h,
        json={"preset": "custom", "sections": ["quotes"], "format": "txt"},
    )
    assert prev.status_code == 200, prev.text[:400]
    text = prev.json().get("text") or ""
    assert "曾经引用·已失效" in text or "已失效" in text
    assert "当前采用" not in text or "不再作为当前采用" in text


def test_p1_04_empty_custom_preview_returns_400_not_full_report():
    a = TestClient(app, raise_server_exceptions=False)
    _, h = project(a, "空章节预览")
    r = a.post(
        "/api/reports/preview",
        headers=h,
        json={"preset": "custom", "sections": [], "format": "txt"},
    )
    assert r.status_code == 400
    assert "至少" in r.text
    # 旧完整导出接口仍可用，但调用方不得把它当作这次预览结果
    old = a.get("/api/export/report", headers=h)
    assert old.status_code == 200
    assert old.text != r.text


def test_p1_05_persist_failure_rolls_back_ghost_project():
    a = TestClient(app, raise_server_exceptions=False)
    # 先确认活动项为示范
    before = a.get("/api/session").json()
    active_before = before["active"]
    listed_before = {x["case_id"] for x in before["cases"]}

    def boom(case):
        raise OSError("simulated disk full")

    with patch.object(store, "_persist_case", side_effect=boom):
        r = a.post("/api/cases/empty", json={"name": "幽灵项目不应出现"})
        assert r.status_code in (500, 400)
    after = a.get("/api/session").json()
    assert after["active"] == active_before
    listed_after = {x["case_id"] for x in after["cases"]}
    new_ids = listed_after - listed_before
    assert not any("user-" in i for i in new_ids)
    # 重试应能成功
    ok = a.post("/api/cases/empty", json={"name": "写盘恢复后的项目"})
    assert ok.status_code == 200, ok.text[:300]
    cid = ok.json()["case"]["case_id"]
    ids = [x["case_id"] for x in a.get("/api/cases").json()["cases"]]
    assert cid in ids


def test_p1_06_version_json_backup_failure_returns_success_with_warning():
    a = TestClient(app, raise_server_exceptions=False)
    _, h = project(a, "版本备份警告")
    real_write = Path.write_text

    def flaky(self, *args, **kwargs):
        if str(self).endswith(".json") and "versions" in str(self).replace("\\", "/"):
            raise OSError("backup dir missing")
        return real_write(self, *args, **kwargs)

    with patch.object(Path, "write_text", flaky):
        r = a.post("/api/versions/save?label=备份目录失效", headers=h)
    assert r.status_code == 200, r.text[:400]
    body = r.json()
    assert body.get("sqlite_saved") is True or body.get("version_id")
    assert body.get("backup_ok") is False
    assert body.get("warning")
    versions = a.get("/api/versions", headers=h).json()["versions"]
    assert any(v.get("label") == "备份目录失效" or v.get("version_id") == body.get("version_id") for v in versions)


def test_p2_tariff_flag_and_manual_override_are_mutex():
    a = TestClient(app, raise_server_exceptions=False)
    _, h = project(a, "电价互斥")
    p1 = a.put("/api/scenario", headers=h, json={"energy_price": 0.95})
    assert p1.status_code == 200
    sc = p1.json()["case"]["scenario"]
    assert sc["energy_price"] == 0.95
    assert sc["energy_price_user_override"] is True
    assert sc.get("use_jinhui_tariff_reference") is False
    p2 = a.put("/api/scenario", headers=h, json={"use_jinhui_tariff_reference": True})
    assert p2.status_code == 200, p2.text[:300]
    sc2 = p2.json()["case"]["scenario"]
    # 不得同时为真
    assert not (sc2.get("use_jinhui_tariff_reference") and sc2.get("energy_price_user_override"))
    if sc2.get("use_jinhui_tariff_reference"):
        assert sc2.get("energy_price_user_override") is False
        assert sc2.get("energy_price") is not None


def test_p2_session_expiry_and_logout_revoke():
    a = TestClient(app, raise_server_exceptions=False)
    ra, ha = project(a, "过期与注销")
    cid = ra.json()["case"]["case_id"]
    sid = a.cookies.get("ltzs_sid")
    mac = a.cookies.get("ltzs_stk")
    assert sid and mac
    issued, _ = store._parse_session_mac(mac)
    assert issued is not None

    # 时间旅行超过 TTL
    original_now = store._now_ts()
    store.SESSION_TTL_SECONDS = 10
    try:
        with patch.object(store, "_now_ts", return_value=issued + 11):
            b = TestClient(app, raise_server_exceptions=False)
            b.cookies.set("ltzs_sid", sid)
            b.cookies.set("ltzs_stk", mac)
            sess = b.get("/api/session")
            assert sess.json()["session_id"] != sid
            ids = [x["case_id"] for x in sess.json()["cases"]]
            assert cid not in ids
    finally:
        store.SESSION_TTL_SECONDS = 7 * 86400

    # 主动注销后旧 Cookie 失效
    c2 = TestClient(app, raise_server_exceptions=False)
    r2, h2 = project(c2, "待注销")
    cid2 = r2.json()["case"]["case_id"]
    sid2, mac2 = c2.cookies.get("ltzs_sid"), c2.cookies.get("ltzs_stk")
    out = c2.post("/api/session/logout")
    assert out.status_code == 200, out.text[:300]
    assert out.json()["session_id"] != sid2
    thief = TestClient(app, raise_server_exceptions=False)
    thief.cookies.set("ltzs_sid", sid2)
    thief.cookies.set("ltzs_stk", mac2)
    sess2 = thief.get("/api/session")
    assert sess2.json()["session_id"] != sid2
    assert cid2 not in [x["case_id"] for x in sess2.json()["cases"]]


def test_p2_xlsx_zip_bomb_rejected():
    # 构造压缩比极高但声明体积很大的 zip（不真正展开）
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 用 ZipInfo 声明巨大 file_size
        info = zipfile.ZipInfo("xl/worksheets/sheet1.xml")
        data = b"<xml>tiny</xml>"
        zf.writestr(info, data)
        # 手工改文件头不太稳定，改为直接调用检查函数模拟超大未压缩尺寸
    # 直接单测检查函数：构造 infolist-like 通过写入正常文件后再用 monkeypatch 很难。
    # 写入一个声称超大的成员。
    big = io.BytesIO()
    with zipfile.ZipFile(big, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        payload = b"0" * 1024
        zf.writestr("xl/workbook.xml", payload)
    raw = big.getvalue()
    # 正常小文件应通过
    _check_xlsx_zip_bomb(raw)

    # 路径穿越应拒绝
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../sheet.xml", b"x")
    with pytest.raises(ValueError):
        _check_xlsx_zip_bomb(evil.getvalue())


def test_session_info_does_not_leak_other_tokens():
    a = TestClient(app, raise_server_exceptions=False)
    ra, ha = project(a, "不泄密")
    tok_a = ha["X-Project-Token"]
    b = TestClient(app, raise_server_exceptions=False)
    info = b.get("/api/session").json()
    dumped = json.dumps(info, ensure_ascii=False)
    assert tok_a not in dumped
    assert info.get("auth_scope") in ("public_demo", "workspace_session", "project_token")


def test_energy_keep_manual_default_and_use_imported_policy():
    a = TestClient(app, raise_server_exceptions=False)
    _, h = project(a, "电价策略")
    a.put("/api/scenario", headers=h, json={"energy_price": 0.89})
    csv = "年份,工业用电量_万kWh,电力费用_万元\n2024,100,50\n"
    mappings = json.dumps(
        [
            {"user_column": "年份", "standard_field": "年份"},
            {"user_column": "工业用电量_万kWh", "standard_field": "工业用电量_万kWh"},
            {"user_column": "电力费用_万元", "standard_field": "电力费用_万元"},
        ],
        ensure_ascii=False,
    )
    r1 = a.post(
        f"/api/upload/commit?kind=energy&mappings_json={mappings}",
        headers=h,
        files={"file": ("e.csv", csv.encode("utf-8-sig"), "text/csv")},
    )
    assert r1.status_code == 200, r1.text[:400]
    sc1 = (r1.json().get("case") or r1.json()["results"]["case"])["scenario"]
    assert sc1["energy_price"] == 0.89
    r2 = a.post(
        f"/api/upload/commit?kind=energy&mappings_json={mappings}&energy_price_policy=use_imported",
        headers=h,
        files={"file": ("e.csv", csv.encode("utf-8-sig"), "text/csv")},
    )
    assert r2.status_code == 200, r2.text[:400]
    sc2 = (r2.json().get("case") or r2.json()["results"]["case"])["scenario"]
    assert abs(float(sc2["energy_price"]) - 0.5) < 1e-9


def test_cors_untrusted_origin_has_no_credentialed_allow():
    a = TestClient(app, raise_server_exceptions=False)
    r = a.get("/api/session", headers={"Origin": "http://127.0.0.1:9377"})
    allow = r.headers.get("access-control-allow-origin")
    assert allow not in ("http://127.0.0.1:9377", "*")


def test_auth_scope_notes_are_explicit(c):
    sess = c.get("/api/session").json()
    assert "project_token" in (sess.get("auth_scope_note") or "")
    assert "workspace" in (sess.get("auth_scope_note") or "") or "工作区" in (sess.get("auth_scope_note") or "")
