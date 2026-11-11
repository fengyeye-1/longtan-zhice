"""V8.2.12 独立封版回归：注销不得被外层中间件重新签发旧会话。

放入 backend/tests/ 下执行：
    cd backend
    python -m pytest -q tests/test_v8212_logout_final_gate.py

本测试使用仓库 conftest.py 的隔离临时数据库，不接触正式企业数据库。
"""
import time
from fastapi.testclient import TestClient
from app.main import app
from app.services import store


def _set_cookie_headers(resp, name: str) -> list[str]:
    getter = getattr(resp.headers, "get_list", None)
    if getter is None:
        getter = resp.headers.getlist
    return [h for h in getter("set-cookie") if h.lower().startswith(name.lower() + "=")]


def _cookie_payload(header: str) -> str:
    return header.split(";", 1)[0].split("=", 1)[1]


def test_logout_fresh_cookie_matches_new_anonymous_session():
    store.reset_all_for_tests()
    with TestClient(app, raise_server_exceptions=False) as browser:
        made = browser.post("/api/cases/empty", json={"name": "封版回归-普通退出"})
        assert made.status_code == 200
        old_sid = browser.cookies.get("ltzs_sid")
        logout = browser.post("/api/session/logout")
        assert logout.status_code == 200
        new_sid = logout.json()["session_id"]
        assert new_sid != old_sid
        assert browser.cookies.get("ltzs_sid") == new_sid, (
            "退出接口返回新SID，但中间件的Set-Cookie覆盖为旧SID"
        )
        assert store.verify_session_mac(old_sid, made.cookies.get("ltzs_stk") or browser.cookies.get("ltzs_stk")) is False
        sid_hs = _set_cookie_headers(logout, "ltzs_sid")
        stk_hs = _set_cookie_headers(logout, "ltzs_stk")
        assert len(sid_hs) == 1, "注销响应应只有一份 ltzs_sid"
        assert len(stk_hs) == 1, "注销响应应只有一份 ltzs_stk"
        assert _cookie_payload(sid_hs[0]) == new_sid


def test_aged_cookie_logout_cannot_reissue_revoked_owner_identity(monkeypatch):
    store.reset_all_for_tests()
    clock = {"now": int(time.time())}
    monkeypatch.setattr(store, "_now_ts", lambda: clock["now"])
    monkeypatch.setattr(store, "SESSION_TTL_SECONDS", 7 * 86400)
    with TestClient(app, raise_server_exceptions=False) as browser:
        made = browser.post("/api/cases/empty", json={"name": "封版回归-长会话退出"})
        assert made.status_code == 200
        case_id = made.json()["case"]["case_id"]
        old_sid = browser.cookies.get("ltzs_sid")
        old_mac = browser.cookies.get("ltzs_stk")
        assert store.verify_session_mac(old_sid, old_mac)

        # 模拟使用4天：超过7天票据有效期的一半，但还没有过期
        clock["now"] += 4 * 86400
        assert store.verify_session_mac(old_sid, old_mac)

        logout = browser.post("/api/session/logout")
        assert logout.status_code == 200
        new_sid = logout.json()["session_id"]
        assert new_sid != old_sid
        assert browser.cookies.get("ltzs_sid") == new_sid, (
            "旧SID重新覆盖浏览器Cookie，可能重新获得所有者权限"
        )
        assert not store.verify_session_mac(old_sid, old_mac), "已注销旧票据仍被接受"

        case_list = browser.get("/api/cases")
        assert case_list.status_code == 200
        assert all(
            x["case_id"] != case_id
            for x in case_list.json()["cases"]
        ), "退出后依然能列出原私人项目"
        activate = browser.post(f"/api/cases/{case_id}/activate")
        assert activate.status_code in (403, 404), (
            "退出后未提供项目Token，仍能重新激活原私人项目"
        )

        sid_hs = _set_cookie_headers(logout, "ltzs_sid")
        stk_hs = _set_cookie_headers(logout, "ltzs_stk")
        assert len(sid_hs) == 1
        assert len(stk_hs) == 1
        assert _cookie_payload(sid_hs[0]) == new_sid
        assert store.verify_session_mac(old_sid, old_mac) is False

        devices = browser.post(
            "/api/devices",
            json={"name": "退出后不应写入", "device_type": "球磨", "power_kw": 10, "hours": 1000},
        )
        # 注销后可能落到公开示范项目，写入示范不等于恢复已退出的私人项目。
        if devices.status_code == 200:
            assert devices.json()["case"]["case_id"] != case_id
        else:
            assert devices.status_code in (400, 403, 404), devices.text[:200]

        with TestClient(app, raise_server_exceptions=False) as replay_cookie:
            replay_cookie.cookies.set("ltzs_sid", old_sid)
            replay_cookie.cookies.set("ltzs_stk", old_mac)
            listed = replay_cookie.get("/api/cases")
            assert listed.status_code == 200
            assert all(x["case_id"] != case_id for x in listed.json()["cases"])
            act = replay_cookie.post(f"/api/cases/{case_id}/activate")
            assert act.status_code in (403, 404)

        with TestClient(app, raise_server_exceptions=False) as replay_header:
            listed = replay_header.get("/api/cases", headers={"X-Session-Id": old_sid})
            assert listed.status_code == 200
            assert all(x["case_id"] != case_id for x in listed.json()["cases"])
            act = replay_header.post(
                f"/api/cases/{case_id}/activate",
                headers={"X-Session-Id": old_sid},
            )
            assert act.status_code in (403, 404)
