"""V8.2.11 权限根因与报告展示自检。仅用于隔离测试库，不触碰真实企业数据。"""
from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import store
from app.version import APP_VERSION, BUILD_ID


PUBLIC_HTTPS = "https://longtan-zhice-73b61.containers.snapdeploy.app"
PUBLIC_HTTP = "http://longtan-zhice-73b61.containers.snapdeploy.app"
CSRF_DETAIL = "拒绝非可信来源的状态修改请求（Origin/CSRF 校验失败）"
KEY = "LTZS_PUBLIC_ORIGIN"
INAPPLICABLE = "超过该设备可用运行小时，不适用"


@pytest.fixture(autouse=True)
def isolated_reset():
    store.reset_all_for_tests()
    store.SESSION_TTL_SECONDS = 7 * 86400
    store.ANON_SESSION_IDLE_SECONDS = 30 * 60
    store.MAX_ANON_SESSIONS = 200
    store._LAST_SESSION_SWEEP_TS = 0
    assert "ltzs_pytest_" in str(store.DB_PATH) or "ltzs_pytest_" in str(store.ROOT)


def _project(client: TestClient, name: str = "8211测试企业"):
    r = client.post("/api/cases/empty", json={"name": name})
    assert r.status_code == 200, r.text[:400]
    body = r.json()
    cid = body["case"]["case_id"]
    tok = body.get("project_token") or r.headers.get("X-Project-Token")
    assert tok
    return r, cid, tok


def _ids(payload) -> set[str]:
    return {x["case_id"] for x in (payload.get("cases") or [])}


def _no_token_leak(resp, cid: str | None = None) -> None:
    """断言响应不得把当前有效项目 Token 交给访客。不把真实 token 写进断言字符串。"""
    header = resp.headers.get("X-Project-Token") or ""
    assert header in ("", None) or False is True if header else True
    assert not header
    body = {}
    try:
        body = resp.json()
    except Exception:
        return
    case = body.get("case") if isinstance(body.get("case"), dict) else body
    token = None
    if isinstance(case, dict):
        token = case.get("access_token")
    if body.get("project_token"):
        token = token or body.get("project_token")
    assert not token
    if cid and isinstance(case, dict) and case.get("case_id") == cid:
        assert "access_token" not in case or not case.get("access_token")


def test_health_version_is_8211():
    c = TestClient(app, raise_server_exceptions=False)
    h = c.get("/api/health").json()
    assert h["version"] == "8.2.13" == APP_VERSION
    assert h["frontend_expected"] == "8.2.13"
    assert h["build_id"] == BUILD_ID == "v8213-audit-20261111"


def test_f01_guest_token_cannot_access_sibling_projects():
    a = TestClient(app, raise_server_exceptions=False)
    _, id1, tok1 = _project(a, "项目甲")
    r2, id2, _ = _project(a, "项目乙")
    add = a.post(
        "/api/devices",
        json={"name": "乙独有设备", "device_type": "球磨", "power_kw": 40, "hours": 2000},
    )
    assert add.status_code == 200, add.text[:300]

    b = TestClient(app, raise_server_exceptions=False)
    sess = b.get("/api/session", headers={"X-Project-Token": tok1})
    assert sess.status_code == 200
    ids = _ids(sess.json())
    assert id1 in ids
    assert id2 not in ids
    for row in sess.json()["cases"]:
        if row["case_id"] == id1:
            assert row.get("owned") is False
    act = b.post(f"/api/cases/{id2}/activate", headers={"X-Project-Token": tok1})
    assert act.status_code in (403, 404)
    got = b.get("/api/case", headers={"X-Project-Token": tok1})
    assert got.status_code == 200
    names = [d["name"] for d in (got.json().get("devices") or [])]
    assert "乙独有设备" not in names
    _no_token_leak(got, id1)
    _no_token_leak(sess)


def test_f02_signed_cookie_does_not_elevate_guest_to_owner():
    a = TestClient(app, raise_server_exceptions=False)
    _, cid, tok = _project(a, "按角色验收")
    owner_sid = a.cookies.get("ltzs_sid")
    b = TestClient(app, raise_server_exceptions=False)
    initial = b.get("/api/session", headers={"X-Project-Token": tok})
    assert initial.status_code == 200
    follow = b.get("/api/session")
    assert follow.status_code == 200
    visitor_sid = b.cookies.get("ltzs_sid")
    assert visitor_sid and visitor_sid != owner_sid
    assert store._CASES[cid].owner_session == owner_sid
    assert store._CASES[cid].owner_session != visitor_sid
    assert cid in _ids(follow.json())
    row = next(x for x in follow.json()["cases"] if x["case_id"] == cid)
    assert row.get("owned") is False
    assert cid not in (store._SESSIONS[visitor_sid].get("owned_case_ids") or set())
    _no_token_leak(follow, cid)


def test_f03_guest_restore_is_forbidden_and_ownership_unchanged():
    a = TestClient(app, raise_server_exceptions=False)
    _, cid, tok = _project(a, "私人测试：版本恢复权限")
    owner_sid = a.cookies.get("ltzs_sid")
    v = a.post("/api/versions/save", params={"label": "备份A"})
    assert v.status_code == 200, v.text[:300]
    vid = v.json().get("version_id")
    assert vid
    b = TestClient(app, raise_server_exceptions=False)
    rr = b.get("/api/results", headers={"X-Project-Token": tok})
    assert rr.status_code == 200
    assert rr.json().get("case", {}).get("case_id") == cid
    listed = b.get("/api/versions")
    assert listed.status_code == 200
    assert any(x["version_id"] == vid for x in listed.json().get("versions", []))
    restored = b.post(f"/api/versions/{vid}/restore")
    assert restored.status_code == 403, restored.text[:300]
    assert store._CASES[cid].owner_session == owner_sid
    assert any(c.get("case_id") == cid for c in a.get("/api/session").json().get("cases", []))


def test_f04_f05_owner_rehydrates_after_restart_guest_does_not_become_owner():
    a = TestClient(app, raise_server_exceptions=False)
    _, cid, tok = _project(a, "重启后所有权")
    owner_sid = a.cookies.get("ltzs_sid")
    a.post("/api/versions/save", params={"label": "重启备份"})
    b = TestClient(app, raise_server_exceptions=False)
    b.get("/api/results", headers={"X-Project-Token": tok})
    b.post(f"/api/versions/{a.get('/api/versions').json()['versions'][0]['version_id']}/restore")
    store._SESSIONS.clear()
    assert cid in _ids(a.get("/api/session").json())
    act = a.post(f"/api/cases/{cid}/activate")
    assert act.status_code == 200, act.text[:300]
    still = a.get("/api/case")
    assert still.status_code == 200
    assert still.json()["case_id"] == cid
    hist = a.get("/api/versions")
    assert hist.status_code == 200
    assert any(x.get("case_id") == cid for x in hist.json().get("versions") or [])
    guest_sess = b.get("/api/session")
    assert cid not in _ids(guest_sess.json())
    assert store._CASES[cid].owner_session == owner_sid
    assert store._CASES[cid].owner_session != b.cookies.get("ltzs_sid")


def test_f06_guest_cannot_rotate_or_revoke_owner_token():
    a = TestClient(app, raise_server_exceptions=False)
    _, cid, old = _project(a, "私人测试：访客能否轮换项目凭证")
    b = TestClient(app, raise_server_exceptions=False)
    b.get("/api/results", headers={"X-Project-Token": old})
    rot = b.post("/api/session/project-token/rotate")
    assert rot.status_code == 403, rot.text[:300]
    assert store._TOKENS.get(old) == cid
    assert store._CASES[cid].access_token == old
    rev = b.post("/api/session/project-token/revoke")
    assert rev.status_code == 403, rev.text[:300]
    assert store._TOKENS.get(old) == cid
    fresh = TestClient(app, raise_server_exceptions=False)
    ok = fresh.get("/api/results", headers={"X-Project-Token": old})
    assert ok.status_code == 200
    assert ok.json().get("case", {}).get("case_id") == cid


def test_f07_f08_rotate_invalidates_guest_cookie_and_does_not_leak_new_token():
    a = TestClient(app, raise_server_exceptions=False)
    _, cid, old = _project(a, "轮换后访客失效")
    b = TestClient(app, raise_server_exceptions=False)
    first = b.get("/api/results", headers={"X-Project-Token": old})
    assert first.status_code == 200
    rot = a.post("/api/session/project-token/rotate")
    assert rot.status_code == 200, rot.text[:300]
    new_tok = rot.json()["project_token"]
    assert new_tok and new_tok != old
    cookie_only = b.get("/api/results")
    if cookie_only.status_code == 200:
        assert cookie_only.json().get("case", {}).get("case_id") != cid
    else:
        assert cookie_only.status_code in (403, 404)
    _no_token_leak(cookie_only)
    header = cookie_only.headers.get("X-Project-Token") or ""
    assert header != new_tok
    body_tok = (cookie_only.json().get("case") or {}).get("access_token") if cookie_only.status_code == 200 else None
    assert body_tok not in (new_tok,)
    # 旧 Token 的新客户端也不得访问
    stale = TestClient(app, raise_server_exceptions=False)
    bad = stale.get("/api/results", headers={"X-Project-Token": old})
    if bad.status_code == 200:
        assert bad.json().get("case", {}).get("case_id") != cid
    else:
        assert bad.status_code in (403, 404)


def test_f09_f10_revoke_blocks_guest_mutations_and_old_token_clients():
    a = TestClient(app, raise_server_exceptions=False)
    _, cid, old = _project(a, "旧凭证访客权限撤销链")
    before = a.get("/api/case").json()
    rev_before = before["revision"]
    dev_before = [d["name"] for d in before.get("devices") or []]
    b = TestClient(app, raise_server_exceptions=False)
    b.get("/api/results", headers={"X-Project-Token": old})
    a.post("/api/session/project-token/rotate")
    a.post("/api/session/project-token/revoke")
    read = b.get("/api/results")
    if read.status_code == 200:
        assert read.json().get("case", {}).get("case_id") != cid
    else:
        assert read.status_code in (403, 404)
    _no_token_leak(read)
    add = b.post("/api/devices", json={"name": "访客撤销后写入测试", "device_type": "球磨", "power_kw": 25, "hours": 500})
    assert add.status_code in (400, 403, 404) or (
        add.status_code == 200 and add.json().get("case", {}).get("case_id") != cid
    )
    owner_case = a.get("/api/case").json()
    assert owner_case["case_id"] == cid
    assert owner_case["revision"] == rev_before
    assert "访客撤销后写入测试" not in [d["name"] for d in owner_case.get("devices") or []]
    assert [d["name"] for d in owner_case.get("devices") or []] == dev_before
    for method, url, kw in (
        ("post", "/api/devices", {"json": {"name": "再写", "device_type": "球磨", "power_kw": 1, "hours": 1}}),
        ("put", "/api/scenario", {"json": {"hours": 1000}}),
    ):
        fn = getattr(b, method)
        resp = fn(url, **kw)
        if resp.status_code == 200:
            assert resp.json().get("case", {}).get("case_id") != cid
        else:
            assert resp.status_code in (400, 403, 404)
    stale = TestClient(app, raise_server_exceptions=False)
    bad = stale.get("/api/results", headers={"X-Project-Token": old})
    if bad.status_code == 200:
        assert bad.json().get("case", {}).get("case_id") != cid
    else:
        assert bad.status_code in (403, 404)


def test_f11_owner_keeps_access_after_rotate_and_revoke():
    a = TestClient(app, raise_server_exceptions=False)
    _, cid, old = _project(a, "所有者仍可进入")
    v = a.post("/api/versions/save", params={"label": "所有者备份"})
    assert v.status_code == 200
    exp = a.post("/api/reports/export", json={"format": "txt", "preset": "full"})
    assert exp.status_code == 200, exp.text[:300]
    rot = a.post("/api/session/project-token/rotate")
    assert rot.status_code == 200
    new_tok = rot.json()["project_token"]
    assert new_tok != old
    assert cid in _ids(a.get("/api/session").json())
    a.post("/api/session/project-token/revoke")
    still = a.get("/api/session")
    assert cid in _ids(still.json())
    act = a.post(f"/api/cases/{cid}/activate")
    assert act.status_code == 200
    versions = a.get("/api/versions").json().get("versions", [])
    assert any(x.get("case_id") == cid for x in versions)
    archives = a.get("/api/reports/archives")
    assert archives.status_code == 200
    assert len(archives.json().get("reports") or []) >= 1


def test_f12_other_private_project_strictly_isolated():
    a = TestClient(app, raise_server_exceptions=False)
    _, ida, toka = _project(a, "项目A")
    a.post("/api/versions/save", params={"label": "A备份"})
    c = TestClient(app, raise_server_exceptions=False)
    _, idc, tokc = _project(c, "项目C")
    c.post("/api/versions/save", params={"label": "C备份"})
    exp = c.post("/api/reports/export", json={"format": "txt", "preset": "one_page"})
    assert exp.status_code == 200
    b = TestClient(app, raise_server_exceptions=False)
    b.get("/api/results", headers={"X-Project-Token": toka})
    sess = b.get("/api/session")
    ids = _ids(sess.json())
    assert ida in ids
    assert idc not in ids
    assert not any(x.get("label") == "C备份" for x in b.get("/api/versions").json().get("versions", []))
    arch = b.get("/api/reports/archives")
    if arch.status_code == 200:
        recs = arch.json().get("reports") or []
        assert all(r.get("case_id") != idc for r in recs)
    outsider = TestClient(app, raise_server_exceptions=False)
    assert idc not in _ids(outsider.get("/api/session").json())
    # A 也不得凭自己的 Token 读 C
    steal = a.get("/api/case", headers={"X-Project-Token": tokc})
    if steal.status_code == 200:
        # A 同时有自己的 Cookie，attach_only 只授权 C 这一请求；不得把 C 并入 A 的 owned
        assert idc not in (store._SESSIONS[a.cookies.get("ltzs_sid")].get("owned_case_ids") or set())
    listed = _ids(a.get("/api/session").json())
    row = next((x for x in a.get("/api/session").json()["cases"] if x["case_id"] == idc), None)
    if row:
        assert row.get("owned") is False


def test_f13_export_has_no_none_placeholder_for_invalid_hours():
    a = TestClient(app, raise_server_exceptions=False)
    _, cid, _ = _project(a, "无效小时报告")
    d = a.post("/api/devices", json={"name": "测试球磨机", "device_type": "球磨", "power_kw": 150, "hours": 4000})
    assert d.status_code == 200, d.text[:300]
    did = [x for x in d.json()["case"]["devices"] if x["name"] == "测试球磨机"][0]["id"]
    m = a.post(
        "/api/retrofits",
        json={"name": "降功率20kW", "device_id": did, "power_drop_kw": 20, "investment_cny": 100000, "annual_maintenance_cny": 0, "select": True},
    )
    assert m.status_code == 200, m.text[:300]
    u = a.put("/api/scenario", json={"hours": 4000, "energy_price": 0.6, "emission_factor": 0.4471})
    assert u.status_code == 200, u.text[:300]
    schemes = (u.json().get("results") or {}).get("schemes") or []
    assert schemes
    over = [s for s in schemes if s.get("hours") and float(s["hours"]) > 4000]
    assert over
    assert all(s.get("inapplicable_reason") == INAPPLICABLE or s.get("error") == INAPPLICABLE for s in over)
    preview = a.get("/api/results").json()
    for s in preview.get("schemes") or []:
        if s.get("hours") and float(s["hours"]) > 4000:
            assert s.get("inapplicable_reason") == INAPPLICABLE
    for fmt in ("txt", "csv", "html"):
        r = a.post("/api/reports/export", json={"format": fmt, "preset": "full"})
        assert r.status_code == 200, r.text[:200]
        text = r.text
        assert "None kWh" not in text
        assert "None t" not in text
        assert "None tCO2" not in text
        assert "null" not in text.lower() or "null" not in re.findall(r"None|NaN|null", text)
        assert "NaN" not in text
        assert INAPPLICABLE in text


def test_f14_power_hours_cost_numbers():
    a = TestClient(app, raise_server_exceptions=False)
    _project(a, "核算20x4000")
    d = a.post("/api/devices", json={"name": "球磨核算", "device_type": "球磨", "power_kw": 150, "hours": 4000})
    assert d.status_code == 200
    did = d.json()["case"]["devices"][0]["id"]
    m = a.post(
        "/api/retrofits",
        json={"name": "降功率20kW", "device_id": did, "power_drop_kw": 20, "investment_cny": 100000, "annual_maintenance_cny": 0, "select": True},
    )
    assert m.status_code == 200
    u = a.put("/api/scenario", json={"hours": 4000, "energy_price": 0.60, "emission_factor": 0.4471})
    assert u.status_code == 200, u.text[:300]
    sa = (u.json().get("results") or {}).get("selected_assessment") or {}
    assert abs(float(sa["saved_kwh"]) - 80000.0) < 1e-6
    assert abs(float(sa["co2_reduction_t"]) - 35.768) < 0.00051
    assert abs(float(sa["annual_cost_saving_cny"]) - 48000.0) < 0.01


def test_f15_illegal_inputs_rejected_and_rolled_back():
    a = TestClient(app, raise_server_exceptions=False)
    _project(a, "非法输入回滚")
    d = a.post("/api/devices", json={"name": "测试球磨机", "device_type": "球磨", "power_kw": 150, "hours": 4000})
    did = d.json()["case"]["devices"][0]["id"]
    m = a.post(
        "/api/retrofits",
        json={"name": "降功率20kW", "device_id": did, "power_drop_kw": 20, "investment_cny": 100000, "annual_maintenance_cny": 0, "select": True},
    )
    assert m.status_code == 200
    a.put("/api/scenario", json={"hours": 4000, "energy_price": 0.6, "emission_factor": 0.4471})
    before = a.get("/api/case").json()
    rev = before["revision"]
    mids = len(before["retrofits"])
    invalid = [
        ("put", "/api/scenario", {"hours": 6000}),
        ("put", "/api/scenario", {"investment_cny": -1}),
        ("post", "/api/retrofits", {"name": "bad", "device_id": did, "saving_fraction": 1.5, "select": True}),
        ("post", "/api/retrofits", {"name": "bad", "device_id": did, "power_drop_kw": 200, "select": True}),
    ]
    for method, url, payload in invalid:
        r = getattr(a, method)(url, json=payload)
        assert r.status_code >= 400, (url, payload, r.status_code, r.text[:200])
        case = a.get("/api/case").json()
        assert case["revision"] == rev
        assert len(case["retrofits"]) == mids


def test_f16_cross_scheme_origin_and_forwarded_proto_rejected():
    prev = os.environ.get(KEY)
    os.environ[KEY] = PUBLIC_HTTPS
    try:
        c = TestClient(app, base_url=PUBLIC_HTTP, raise_server_exceptions=False)
        res = c.post(
            "/api/reset-demo",
            headers={"Origin": PUBLIC_HTTP, "Content-Type": "application/json"},
            json={},
        )
        assert res.status_code == 403, res.text[:400]
        assert CSRF_DETAIL in str(res.json().get("detail"))
        spoof = c.post(
            "/api/reset-demo",
            headers={
                "Origin": PUBLIC_HTTP,
                "X-Forwarded-Proto": "https",
                "Content-Type": "application/json",
            },
            json={},
        )
        assert spoof.status_code == 403, spoof.text[:400]
        ok = c.post(
            "/api/cases/empty",
            headers={"Origin": PUBLIC_HTTPS, "Content-Type": "application/json"},
            json={"name": "可信来源"},
        )
        assert ok.status_code == 200, ok.text[:300]
    finally:
        if prev is None:
            os.environ.pop(KEY, None)
        else:
            os.environ[KEY] = prev
    e = TestClient(app, base_url="https://longtan.example", raise_server_exceptions=False)
    res = e.post(
        "/api/cases/empty",
        json={"name": "跨协议应拒绝"},
        headers={"Origin": "http://longtan.example"},
    )
    assert res.status_code == 403, res.text[:300]


def test_extra_memory_clear_does_not_revive_guest_grant():
    a = TestClient(app, raise_server_exceptions=False)
    _, cid, tok = _project(a, "清空内存后访客")
    b = TestClient(app, raise_server_exceptions=False)
    b.get("/api/results", headers={"X-Project-Token": tok})
    store._SESSIONS.clear()
    guest = b.get("/api/results")
    if guest.status_code == 200:
        assert guest.json().get("case", {}).get("case_id") != cid
    else:
        assert guest.status_code in (403, 404)
    # 旧 grant 不得在空内存中被重新水合为所有者
    assert store._CASES[cid].owner_session == a.cookies.get("ltzs_sid")
    assert cid in _ids(a.get("/api/session").json())


def test_extra_owner_sid_plus_foreign_token_does_not_transfer_ownership():
    a = TestClient(app, raise_server_exceptions=False)
    _, ida, _ = _project(a, "甲工作区")
    b = TestClient(app, raise_server_exceptions=False)
    _, idb, tokb = _project(b, "乙工作区")
    mixed = TestClient(app, raise_server_exceptions=False)
    mixed.cookies.set("ltzs_sid", a.cookies.get("ltzs_sid"))
    mixed.cookies.set("ltzs_stk", a.cookies.get("ltzs_stk"))
    sess = mixed.get("/api/session", headers={"X-Project-Token": tokb})
    assert sess.status_code == 200
    ids = _ids(sess.json())
    assert set(ids) <= {ida, idb, "jinhui-demo"}
    assert store._CASES[idb].owner_session == b.cookies.get("ltzs_sid")
    assert idb not in (store._SESSIONS[a.cookies.get("ltzs_sid")].get("owned_case_ids") or set())
    rot = mixed.post("/api/session/project-token/rotate", headers={"X-Project-Token": tokb})
    # 他人 Token 不得抢占甲的活动项目，因此该请求若成功也只能轮换甲自己的凭证，乙的 Token 必须原样有效。
    if rot.status_code == 200:
        assert rot.json().get("project_token") != tokb
        assert rot.json().get("active") == ida or a.get("/api/session").json().get("active") == ida
    else:
        assert rot.status_code == 403
    assert store._TOKENS.get(tokb) == idb
    assert store._CASES[idb].access_token == tokb
    still_b = TestClient(app, raise_server_exceptions=False)
    okb = still_b.get("/api/results", headers={"X-Project-Token": tokb})
    assert okb.status_code == 200
    assert okb.json().get("case", {}).get("case_id") == idb


def test_extra_inherited_hours_show_source_not_unfilled():
    a = TestClient(app, raise_server_exceptions=False)
    _project(a, "小时继承")
    d = a.post("/api/devices", json={"name": "带小时球磨", "device_type": "球磨", "power_kw": 20, "hours": 4000})
    did = d.json()["case"]["devices"][0]["id"]
    m = a.post(
        "/api/retrofits",
        json={"name": "降功率5kW", "device_id": did, "power_drop_kw": 5, "investment_cny": 10000, "annual_maintenance_cny": 0, "select": True},
    )
    assert m.status_code == 200
    # 不填写情景小时，应继承设备小时
    case = a.get("/api/case").json()
    assert case["scenario"].get("hours") in (None, "")
    prep = case.get("data_prep") or []
    hours_item = next(x for x in prep if x.get("key") == "hours")
    assert hours_item["status"] != "needed"
    assert "4000" in str(hours_item["detail"])
    assert "设备" in str(hours_item["detail"])
    assert "完全未填写" not in str(hours_item["detail"])
    assert "请填写年有效节能小时" not in str(hours_item["detail"])
    results = a.get("/api/results").json()
    sa = results.get("selected_assessment") or {}
    assert abs(float(sa.get("saved_kwh") or 0) - 20000.0) < 1e-6
    hours_src = ((sa.get("trace") or {}).get("param_sources") or {}).get("hours") or {}
    if not hours_src:
        hours_src = ((sa.get("source_trace") or {}).get("hours") or {})
    assert hours_src.get("value") == 4000 or hours_src.get("value") == 4000.0
    label = str(hours_src.get("label") or "") + str(hours_src.get("source_name") or "")
    assert "设备" in label
    assert "完全未填写" not in label
