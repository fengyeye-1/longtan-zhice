"""V8.2.13 注销封板与报告展示自检。使用隔离临时库，不接触正式企业数据库。"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import store
from app.version import APP_VERSION, BUILD_ID

INAPPLICABLE = "超过该设备可用运行小时，不适用"


@pytest.fixture(autouse=True)
def isolated_reset():
    store.reset_all_for_tests()
    store.SESSION_TTL_SECONDS = 7 * 86400
    store.ANON_SESSION_IDLE_SECONDS = 30 * 60
    store.MAX_ANON_SESSIONS = 200
    store._LAST_SESSION_SWEEP_TS = 0
    assert "ltzs_pytest_" in str(store.DB_PATH) or "ltzs_pytest_" in str(store.ROOT)


def _set_cookie_headers(resp, name: str) -> list[str]:
    getter = getattr(resp.headers, "get_list", None)
    if getter is None:
        getter = resp.headers.getlist
    return [h for h in getter("set-cookie") if h.lower().startswith(name.lower() + "=")]


def _cookie_payload(header: str) -> str:
    return header.split(";", 1)[0].split("=", 1)[1]


def _ids_in(payload, case_id: str) -> bool:
    return any(x.get("case_id") == case_id for x in (payload or {}).get("cases") or [])


def test_health_version_is_8213_and_sets_no_cookies():
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/api/health")
    assert r.status_code == 200
    h = r.json()
    assert h["version"] == "8.2.13" == APP_VERSION
    assert h["frontend_expected"] == "8.2.13"
    assert h["build_id"] == BUILD_ID == "v8213-audit-20261111"
    assert _set_cookie_headers(r, "ltzs_sid") == []
    assert _set_cookie_headers(r, "ltzs_stk") == []
    assert "set-cookie" not in {k.lower() for k in r.headers.keys()}
    assert c.cookies.get("ltzs_sid") in (None, "")
    assert c.cookies.get("ltzs_stk") in (None, "")


def test_logout_json_sid_matches_single_set_cookie_and_old_ticket_dead():
    with TestClient(app, raise_server_exceptions=False) as browser:
        made = browser.post("/api/cases/empty", json={"name": "8213注销一致企业"})
        assert made.status_code == 200
        case_id = made.json()["case"]["case_id"]
        old_sid = browser.cookies.get("ltzs_sid")
        old_mac = browser.cookies.get("ltzs_stk")
        assert old_sid and old_mac
        assert store.verify_session_mac(old_sid, old_mac)

        logout = browser.post("/api/session/logout")
        assert logout.status_code == 200
        new_sid = logout.json()["session_id"]
        assert new_sid and new_sid != old_sid
        sid_hs = _set_cookie_headers(logout, "ltzs_sid")
        stk_hs = _set_cookie_headers(logout, "ltzs_stk")
        assert len(sid_hs) == 1
        assert len(stk_hs) == 1
        assert _cookie_payload(sid_hs[0]) == new_sid
        assert browser.cookies.get("ltzs_sid") == new_sid
        assert store.verify_session_mac(old_sid, old_mac) is False

        listed = browser.get("/api/cases")
        assert listed.status_code == 200
        assert not _ids_in(listed.json(), case_id)
        activate = browser.post(f"/api/cases/{case_id}/activate")
        assert activate.status_code in (403, 404)
        devices = browser.post(
            "/api/devices",
            json={"name": "注销后写入", "device_type": "球磨", "power_kw": 10, "hours": 1000},
        )
        if devices.status_code == 200:
            assert devices.json()["case"]["case_id"] != case_id
        else:
            assert devices.status_code in (400, 403, 404)


def test_logout_after_renewal_window_cannot_restore_with_cookie_or_header(monkeypatch):
    clock = {"now": int(time.time())}
    monkeypatch.setattr(store, "_now_ts", lambda: clock["now"])
    monkeypatch.setattr(store, "SESSION_TTL_SECONDS", 7 * 86400)
    with TestClient(app, raise_server_exceptions=False) as browser:
        made = browser.post("/api/cases/empty", json={"name": "8213续期窗口注销企业"})
        assert made.status_code == 200
        case_id = made.json()["case"]["case_id"]
        old_sid = browser.cookies.get("ltzs_sid")
        old_mac = browser.cookies.get("ltzs_stk")
        clock["now"] += 4 * 86400
        assert store.verify_session_mac(old_sid, old_mac)

        logout = browser.post("/api/session/logout")
        assert logout.status_code == 200
        new_sid = logout.json()["session_id"]
        assert browser.cookies.get("ltzs_sid") == new_sid != old_sid
        assert len(_set_cookie_headers(logout, "ltzs_sid")) == 1
        assert len(_set_cookie_headers(logout, "ltzs_stk")) == 1
        assert store.verify_session_mac(old_sid, old_mac) is False
        # 即使有人拿旧 SID 去续期，也不得重新签发可验证票据
        assert store.maybe_rotate_session_mac(old_sid, old_mac) in ("", None)
        assert store.verify_session_mac(old_sid, old_mac) is False

        listed = browser.get("/api/cases")
        assert not _ids_in(listed.json(), case_id)
        assert browser.post(f"/api/cases/{case_id}/activate").status_code in (403, 404)
        devices = browser.post(
            "/api/devices",
            json={"name": "续期后写入", "device_type": "球磨", "power_kw": 10, "hours": 1000},
        )
        if devices.status_code == 200:
            assert devices.json()["case"]["case_id"] != case_id
        else:
            assert devices.status_code in (400, 403, 404)

        with TestClient(app, raise_server_exceptions=False) as replay_cookie:
            replay_cookie.cookies.set("ltzs_sid", old_sid)
            replay_cookie.cookies.set("ltzs_stk", old_mac)
            assert not _ids_in(replay_cookie.get("/api/cases").json(), case_id)
            assert replay_cookie.post(f"/api/cases/{case_id}/activate").status_code in (403, 404)

        with TestClient(app, raise_server_exceptions=False) as replay_header:
            headers = {"X-Session-Id": old_sid}
            assert not _ids_in(replay_header.get("/api/cases", headers=headers).json(), case_id)
            assert replay_header.post(
                f"/api/cases/{case_id}/activate", headers=headers
            ).status_code in (403, 404)


def test_still_logged_in_owner_keeps_project_after_other_session_logout():
    with TestClient(app, raise_server_exceptions=False) as owner:
        made = owner.post("/api/cases/empty", json={"name": "8213未注销所有者企业"})
        assert made.status_code == 200
        case_id = made.json()["case"]["case_id"]
        token = made.json().get("project_token") or made.headers.get("X-Project-Token")
        assert token
        listed = owner.get("/api/cases")
        assert _ids_in(listed.json(), case_id)
        assert owner.post(f"/api/cases/{case_id}/activate").status_code == 200

        with TestClient(app, raise_server_exceptions=False) as other:
            other.post("/api/cases/empty", json={"name": "另一个工作区"})
            other.post("/api/session/logout")

        listed = owner.get("/api/cases")
        assert listed.status_code == 200
        assert _ids_in(listed.json(), case_id)
        act = owner.post(f"/api/cases/{case_id}/activate")
        assert act.status_code == 200
        dev = owner.post(
            "/api/devices",
            json={"name": "所有者仍可写入", "device_type": "球磨", "power_kw": 150, "hours": 4000},
        )
        assert dev.status_code == 200, dev.text[:200]


def test_export_empty_tariff_and_cover_decimal_still_hold():
    a = TestClient(app, raise_server_exceptions=False)
    r = a.post("/api/cases/empty", json={"name": "8213报告展示企业"})
    assert r.status_code == 200
    d = a.post(
        "/api/devices",
        json={"name": "公网页面核验球磨", "device_type": "球磨", "power_kw": 150, "hours": 4000},
    )
    assert d.status_code == 200, d.text[:300]
    did = [x for x in d.json()["case"]["devices"] if x["name"] == "公网页面核验球磨"][0]["id"]
    m = a.post(
        "/api/retrofits",
        json={
            "name": "降低功率 20kW",
            "device_id": did,
            "power_drop_kw": 20,
            "investment_cny": 100000,
            "annual_maintenance_cny": 0,
            "select": True,
        },
    )
    assert m.status_code == 200, m.text[:300]
    u = a.put("/api/scenario", json={"hours": 4000, "emission_factor": 0.4471, "energy_price": 0.60, "use_jinhui_tariff_reference": False})
    assert u.status_code == 200, u.text[:300]
    sa = (u.json().get("results") or {}).get("selected_assessment") or {}
    assert abs(float(sa["saved_kwh"]) - 80000.0) < 1e-6
    assert abs(float(sa["co2_reduction_t"]) - 35.768) < 0.00051
    assert abs(float(sa["annual_cost_saving_cny"]) - 48000.0) < 0.01

    txt = a.post("/api/reports/export", json={"format": "txt", "preset": "full"})
    assert txt.status_code == 200
    text = txt.text
    assert "所用电价：0.6 元/kWh" in text
    assert "所用电价：0.6 元/kWh（" not in text
    assert "元/kWh（）" not in text
    assert "80000" in text or "80,000" in text
    assert "35.768" in text
    assert "48000" in text or "48,000" in text
    assert "8e+04" not in text
    assert INAPPLICABLE in text

    html = a.post("/api/reports/export", json={"format": "html", "preset": "full"})
    assert html.status_code == 200
    body = html.text
    assert "元/kWh（）" not in body
    assert "8e+04" not in body
    assert "一年预计省电：80000.0 kWh" in body
    assert "35.768" in body
    assert "一年预计节约电费：48000.0 元" in body
    import re
    cover = re.search(r"<th>预计一年省电</th><td>([^<]+)</td>", body)
    assert cover, body[:800]
    assert cover.group(1).strip() in {"80,000 kWh", "80000 kWh", "80000.0 kWh"}
