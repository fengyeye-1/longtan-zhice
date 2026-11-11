"""V8.2.9 会话撤销、Origin、健康检查与项目凭证自检。仅用于隔离测试库。"""
from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.main import app
from app.services import store
from app.version import APP_VERSION, BUILD_ID


@pytest.fixture(autouse=True)
def isolated_reset():
    store.reset_all_for_tests()
    store.SESSION_TTL_SECONDS = 7 * 86400
    store.ANON_SESSION_IDLE_SECONDS = 30 * 60
    store.MAX_ANON_SESSIONS = 200
    store._LAST_SESSION_SWEEP_TS = 0
    assert "ltzs_pytest_" in str(store.DB_PATH) or "ltzs_pytest_" in str(store.ROOT)


def _legacy_mac(sid: str) -> str:
    return hmac.new(store._session_secret(), sid.encode("utf-8"), hashlib.sha256).hexdigest()


def _ticket_count() -> int:
    conn = sqlite3.connect(store.DB_PATH)
    try:
        return conn.execute("SELECT COUNT(*) FROM session_tickets").fetchone()[0]
    finally:
        conn.close()


def _project(client: TestClient, name: str = "829测试企业"):
    r = client.post("/api/cases/empty", json={"name": name})
    assert r.status_code == 200, r.text[:400]
    return r, r.json()["case"]["case_id"], r.headers.get("X-Project-Token")


def test_health_version_is_829():
    c = TestClient(app, raise_server_exceptions=False)
    h = c.get("/api/health").json()
    assert h["version"] == "8.2.12" == APP_VERSION
    assert h["frontend_expected"] == "8.2.12"
    assert h["build_id"] == BUILD_ID == "v8212-audit-20261111"
    assert h.get("data_persistence") == "local_sqlite"
    note = h.get("data_persistence_note") or ""
    assert "本机" in note
    assert "15 分钟" in note
    assert "60" in note and "90" in note
    assert "新容器" in note
    assert "不能保证" in note
    assert "不能承诺" in note or "持续保存" in note
    assert "TXT" in note and "CSV" in note and "HTML" in note
    assert "SnapDeploy" in note and "Render" in note
    assert "LTZS_COOKIE_SECURE=1" in note
    assert "LTZS_PUBLIC_ORIGIN" in note
    assert "公开代码仓库" in note
    # 不得把健康检查写成“休眠一定清空”或“休眠一定不丢”
    assert "一定会清空" not in note
    assert "一定不会" not in note


def test_a01_legacy_unsigned_issued_at_format_is_rejected():
    a = TestClient(app, raise_server_exceptions=False)
    r, cid, _tok = _project(a, "旧格式不再兼容")
    sid = a.cookies.get("ltzs_sid")
    assert sid
    legacy = _legacy_mac(sid)
    assert store.verify_session_mac(sid, legacy) is False
    b = TestClient(app, raise_server_exceptions=False)
    b.cookies.set("ltzs_sid", sid)
    b.cookies.set("ltzs_stk", legacy)
    sess = b.get("/api/session")
    assert sess.status_code == 200
    assert sess.json()["session_id"] != sid
    assert cid not in [x["case_id"] for x in sess.json()["cases"]]
    assert sess.json().get("auth_scope") != "workspace_session"


def test_a02_legacy_cookie_replay_blocked_after_logout():
    a = TestClient(app, raise_server_exceptions=False)
    r, cid, _tok = _project(a, "注销后旧签名")
    sid = a.cookies.get("ltzs_sid")
    legacy = _legacy_mac(sid)
    out = a.post("/api/session/logout")
    assert out.status_code == 200
    b = TestClient(app, raise_server_exceptions=False)
    b.cookies.set("ltzs_sid", sid)
    b.cookies.set("ltzs_stk", legacy)
    sess = b.get("/api/session")
    assert sess.json()["session_id"] != sid
    assert cid not in [x["case_id"] for x in sess.json()["cases"]]


def test_a03_legacy_cookie_replay_blocked_after_restart():
    a = TestClient(app, raise_server_exceptions=False)
    r, cid, _tok = _project(a, "重启后旧签名")
    sid = a.cookies.get("ltzs_sid")
    legacy = _legacy_mac(sid)
    store._SESSIONS.clear()
    b = TestClient(app, raise_server_exceptions=False)
    b.cookies.set("ltzs_sid", sid)
    b.cookies.set("ltzs_stk", legacy)
    sess = b.get("/api/session")
    assert sess.json()["session_id"] != sid
    assert cid not in [x["case_id"] for x in sess.json()["cases"]]


def test_a04_new_format_legal_restore_after_restart_still_works():
    a = TestClient(app, raise_server_exceptions=False)
    r, cid, _tok = _project(a, "新格式合法恢复")
    a.post("/api/devices", json={"name": "恢复后仍在", "device_type": "通用", "power_kw": 80, "hours": 2000})
    sid, mac = a.cookies.get("ltzs_sid"), a.cookies.get("ltzs_stk")
    assert sid and mac and "." in mac
    store._SESSIONS.clear()
    b = TestClient(app, raise_server_exceptions=False)
    b.cookies.set("ltzs_sid", sid)
    b.cookies.set("ltzs_stk", mac)
    sess = b.get("/api/session")
    assert sess.status_code == 200
    assert sess.json()["session_id"] == sid
    assert cid in [x["case_id"] for x in sess.json()["cases"]]
    act = b.post(f"/api/cases/{cid}/activate")
    assert act.status_code == 200
    case = act.json().get("case") or {}
    assert any(d["name"] == "恢复后仍在" for d in case.get("devices") or [])


def test_a04_new_format_rejected_after_logout_and_expiry():
    a = TestClient(app, raise_server_exceptions=False)
    r, cid, _tok = _project(a, "新格式注销与过期")
    sid, mac = a.cookies.get("ltzs_sid"), a.cookies.get("ltzs_stk")
    issued, _ = store._parse_session_mac(mac)
    assert issued is not None
    out = a.post("/api/session/logout")
    assert out.status_code == 200
    thief = TestClient(app, raise_server_exceptions=False)
    thief.cookies.set("ltzs_sid", sid)
    thief.cookies.set("ltzs_stk", mac)
    sess = thief.get("/api/session")
    assert sess.json()["session_id"] != sid
    assert cid not in [x["case_id"] for x in sess.json()["cases"]]

    b = TestClient(app, raise_server_exceptions=False)
    r2, cid2, _ = _project(b, "新格式过期")
    sid2, mac2 = b.cookies.get("ltzs_sid"), b.cookies.get("ltzs_stk")
    issued2, _ = store._parse_session_mac(mac2)
    store.SESSION_TTL_SECONDS = 10
    try:
        with patch.object(store, "_now_ts", return_value=issued2 + 11):
            c2 = TestClient(app, raise_server_exceptions=False)
            c2.cookies.set("ltzs_sid", sid2)
            c2.cookies.set("ltzs_stk", mac2)
            sess2 = c2.get("/api/session")
            assert sess2.json()["session_id"] != sid2
            assert cid2 not in [x["case_id"] for x in sess2.json()["cases"]]
    finally:
        store.SESSION_TTL_SECONDS = 7 * 86400


def test_a06_project_token_survives_logout_until_rotated_or_revoked():
    a = TestClient(app, raise_server_exceptions=False)
    r, cid, tok = _project(a, "凭证语义")
    assert tok
    out = a.post("/api/session/logout")
    assert out.status_code == 200
    assert out.json().get("token_revoked") is False
    other = TestClient(app, raise_server_exceptions=False)
    got = other.get("/api/results", headers={"X-Project-Token": tok})
    assert got.status_code == 200
    assert got.json().get("case", {}).get("case_id") == cid

    # 8.2.11：仅凭项目 Token 的访客不得轮换所有者凭证；注销后旧 Token 仍可访问，直到所有者轮换/撤销。
    rot = other.post("/api/session/project-token/rotate", headers={"X-Project-Token": tok})
    assert rot.status_code == 403, rot.text[:300]
    stale = TestClient(app, raise_server_exceptions=False)
    still = stale.get("/api/results", headers={"X-Project-Token": tok})
    assert still.status_code == 200
    assert still.json().get("case", {}).get("case_id") == cid


def test_a06_workspace_revoke_invalidates_old_token_but_keeps_workspace():
    a = TestClient(app, raise_server_exceptions=False)
    r, cid, tok = _project(a, "工作区撤销凭证")
    rev = a.post("/api/session/project-token/revoke")
    assert rev.status_code == 200, rev.text[:300]
    stale = TestClient(app, raise_server_exceptions=False)
    bad = stale.get("/api/results", headers={"X-Project-Token": tok})
    if bad.status_code == 200:
        assert bad.json().get("case", {}).get("case_id") != cid
    else:
        assert bad.status_code in (403, 404)
    # 原工作区 Cookie 仍可访问该项目
    still = a.get("/api/session")
    assert cid in [x["case_id"] for x in still.json()["cases"]]
    act = a.post(f"/api/cases/{cid}/activate")
    assert act.status_code == 200


def test_a10_health_does_not_create_session_or_ticket():
    start_sessions = len(store._SESSIONS)
    start_tickets = _ticket_count()
    for _ in range(50):
        with TestClient(app, raise_server_exceptions=False) as anon:
            h = anon.get("/api/health")
            assert h.status_code == 200
            assert h.json()["ok"] is True
            assert not anon.cookies.get("ltzs_sid")
            assert not anon.cookies.get("ltzs_stk")
    assert len(store._SESSIONS) == start_sessions
    assert _ticket_count() == start_tickets


def test_a10_expired_anon_sessions_and_tickets_are_swept_valid_kept():
    a = TestClient(app, raise_server_exceptions=False)
    r, cid, tok = _project(a, "清扫不得误删")
    sid = a.cookies.get("ltzs_sid")
    mac = a.cookies.get("ltzs_stk")
    before_tickets = _ticket_count()
    assert before_tickets >= 1

    anon_sids = []
    for i in range(8):
        with TestClient(app, raise_server_exceptions=False) as anon:
            s = anon.get("/api/session")
            assert s.status_code == 200
            anon_sids.append(s.json()["session_id"])
    assert len(store._SESSIONS) >= 9

    issued, _ = store._parse_session_mac(mac)
    # 把一张非工作区票据改成已过期，验证清扫只删过期票据。
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute(
        "INSERT INTO session_tickets(ticket_id, sid, issued_at, expires_at, revoked) VALUES(?,?,?,?,?)",
        ("expired-anon-ticket", "anon-expired", issued - 100, issued - 10, 1),
    )
    conn.commit()
    conn.close()
    owner_tickets_before = sqlite3.connect(store.DB_PATH)
    owner_rows = owner_tickets_before.execute(
        "SELECT ticket_id FROM session_tickets WHERE sid=?", (sid,)
    ).fetchall()
    owner_tickets_before.close()
    assert owner_rows
    store.ANON_SESSION_IDLE_SECONDS = 5
    try:
        future = issued + 11
        # 把匿名会话的 last_seen 拨到过去，工作区会话保持“刚刚活跃”
        for anon_sid in anon_sids:
            if anon_sid in store._SESSIONS:
                store._SESSIONS[anon_sid]["last_seen"] = issued - 60
        store._SESSIONS[sid]["last_seen"] = future
        with patch.object(store, "_now_ts", return_value=future):
            result = store.sweep_expired_session_state()
            assert result["evicted_sessions"] >= 1
            assert result["deleted_tickets"] >= 1
            assert sid in store._SESSIONS
            assert cid in (store._SESSIONS[sid].get("owned_case_ids") or set())
            leftover_anon = [s for s in anon_sids if s in store._SESSIONS]
            assert leftover_anon == []
            conn = sqlite3.connect(store.DB_PATH)
            still = conn.execute("SELECT ticket_id FROM session_tickets WHERE sid=?", (sid,)).fetchall()
            gone = conn.execute("SELECT ticket_id FROM session_tickets WHERE ticket_id=?", ("expired-anon-ticket",)).fetchone()
            conn.close()
            assert still
            assert gone is None
    finally:
        store.ANON_SESSION_IDLE_SECONDS = 30 * 60

    # 过期后原 Cookie 不得恢复；项目仍在库中，可用原工作区？内存会话还在但票据已过期/删除
    b = TestClient(app, raise_server_exceptions=False)
    b.cookies.set("ltzs_sid", sid)
    b.cookies.set("ltzs_stk", mac)
    with patch.object(store, "_now_ts", return_value=issued + 11):
        store.SESSION_TTL_SECONDS = 10
        try:
            sess = b.get("/api/session")
            assert sess.json()["session_id"] != sid
        finally:
            store.SESSION_TTL_SECONDS = 7 * 86400


def test_a10_capacity_limit_evicts_oldest_anon_only():
    a = TestClient(app, raise_server_exceptions=False)
    r, cid, _ = _project(a, "容量上限保工作区")
    owner = a.cookies.get("ltzs_sid")
    store.MAX_ANON_SESSIONS = 3
    store._LAST_SESSION_SWEEP_TS = 0
    try:
        for _ in range(6):
            with TestClient(app, raise_server_exceptions=False) as anon:
                anon.get("/api/session")
        store.sweep_expired_session_state()
        anons = [sid for sid, sess in store._SESSIONS.items() if store._is_anonymous_session(sess)]
        assert len(anons) <= 3
        assert owner in store._SESSIONS
        assert cid in (store._SESSIONS[owner].get("owned_case_ids") or set())
    finally:
        store.MAX_ANON_SESSIONS = 200


def test_a07_cross_scheme_same_host_origin_rejected():
    e = TestClient(app, base_url="https://longtan.example", raise_server_exceptions=False)
    res = e.post(
        "/api/cases/empty",
        json={"name": "跨协议应拒绝"},
        headers={"Origin": "http://longtan.example"},
    )
    assert res.status_code == 403, res.text[:300]


def test_a08_local_http_same_origin_and_allowlist_succeed():
    a = TestClient(app, raise_server_exceptions=False)
    ok = a.post(
        "/api/cases/empty",
        json={"name": "本机同源"},
        headers={"Origin": "http://testserver", "Content-Type": "application/json"},
    )
    assert ok.status_code == 200, ok.text[:300]
    ok2 = a.post(
        "/api/reset-demo",
        headers={"Origin": "http://127.0.0.1:8000", "Content-Type": "application/json"},
        json={},
    )
    assert ok2.status_code == 200, ok2.text[:300]


def test_a08_configured_public_https_origin_succeeds():
    key = "LTZS_PUBLIC_ORIGIN"
    prev = os.environ.get(key)
    os.environ[key] = "https://longtan-zhice-73b61.containers.snapdeploy.app"
    try:
        a = TestClient(app, raise_server_exceptions=False)
        ok = a.post(
            "/api/cases/empty",
            json={"name": "配置的公网来源"},
            headers={
                "Origin": "https://longtan-zhice-73b61.containers.snapdeploy.app",
                "Content-Type": "application/json",
            },
        )
        assert ok.status_code == 200, ok.text[:300]
    finally:
        if prev is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prev


def test_a07_illegal_origin_still_rejected():
    a = TestClient(app, raise_server_exceptions=False)
    r, cid, _ = _project(a, "非法来源")
    bad = a.post(
        "/api/reset-demo",
        headers={
            "Origin": "http://127.0.0.1:9377",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data="x=1",
    )
    assert bad.status_code == 403
    assert a.get("/api/session").json()["active"] == cid
