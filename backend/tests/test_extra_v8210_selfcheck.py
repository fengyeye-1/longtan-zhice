"""V8.2.10 CSRF：反代 HTTP 下跨协议同主机 Origin 必须拒绝。仅用于隔离测试库。"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import store
from app.version import APP_VERSION, BUILD_ID


PUBLIC_HTTPS = "https://longtan-zhice-73b61.containers.snapdeploy.app"
PUBLIC_HTTP = "http://longtan-zhice-73b61.containers.snapdeploy.app"
PUBLIC_HOST = "longtan-zhice-73b61.containers.snapdeploy.app"
CSRF_DETAIL = "拒绝非可信来源的状态修改请求（Origin/CSRF 校验失败）"
KEY = "LTZS_PUBLIC_ORIGIN"


@pytest.fixture(autouse=True)
def isolated_reset():
    store.reset_all_for_tests()
    store.SESSION_TTL_SECONDS = 7 * 86400
    store.ANON_SESSION_IDLE_SECONDS = 30 * 60
    store.MAX_ANON_SESSIONS = 200
    store._LAST_SESSION_SWEEP_TS = 0
    assert "ltzs_pytest_" in str(store.DB_PATH) or "ltzs_pytest_" in str(store.ROOT)


def _set_public_origin():
    prev = os.environ.get(KEY)
    os.environ[KEY] = PUBLIC_HTTPS
    return prev


def _restore_public_origin(prev):
    if prev is None:
        os.environ.pop(KEY, None)
    else:
        os.environ[KEY] = prev


def _proxy_client() -> TestClient:
    # 模拟 TLS 终止后容器只看到 HTTP，但 Host 仍是公网主机。
    return TestClient(
        app,
        base_url=PUBLIC_HTTP,
        raise_server_exceptions=False,
    )


def _csrf_rejected(res):
    assert res.status_code == 403, res.text[:400]
    detail = res.json().get("detail")
    assert CSRF_DETAIL in str(detail)


def test_health_version_is_8210():
    c = TestClient(app, raise_server_exceptions=False)
    h = c.get("/api/health").json()
    assert h["version"] == "8.2.12" == APP_VERSION
    assert h["frontend_expected"] == "8.2.12"
    assert h["build_id"] == BUILD_ID == "v8212-audit-20261111"


def test_proxy_http_cross_scheme_origin_rejected_on_reset_demo():
    prev = _set_public_origin()
    try:
        c = _proxy_client()
        res = c.post(
            "/api/reset-demo",
            headers={"Origin": PUBLIC_HTTP, "Content-Type": "application/json"},
            json={},
        )
        _csrf_rejected(res)
    finally:
        _restore_public_origin(prev)


def test_proxy_http_cross_scheme_origin_rejected_on_empty_case():
    prev = _set_public_origin()
    try:
        c = _proxy_client()
        res = c.post(
            "/api/cases/empty",
            json={"name": "跨协议同主机应拒绝"},
            headers={"Origin": PUBLIC_HTTP, "Content-Type": "application/json"},
        )
        _csrf_rejected(res)
    finally:
        _restore_public_origin(prev)


def test_proxy_http_configured_https_origin_accepted():
    prev = _set_public_origin()
    try:
        c = _proxy_client()
        res = c.post(
            "/api/cases/empty",
            json={"name": "配置的 https 公网来源应通过"},
            headers={"Origin": PUBLIC_HTTPS, "Content-Type": "application/json"},
        )
        assert res.status_code == 200, res.text[:400]
        reset = c.post(
            "/api/reset-demo",
            headers={"Origin": PUBLIC_HTTPS, "Content-Type": "application/json"},
            json={},
        )
        assert reset.status_code == 200, reset.text[:400]
    finally:
        _restore_public_origin(prev)


def test_proxy_http_illegal_origin_still_rejected():
    prev = _set_public_origin()
    try:
        c = _proxy_client()
        res = c.post(
            "/api/reset-demo",
            headers={"Origin": "https://evil.example", "Content-Type": "application/json"},
            json={},
        )
        _csrf_rejected(res)
        res2 = c.post(
            "/api/cases/empty",
            json={"name": "非法来源"},
            headers={"Origin": "http://evil.example", "Content-Type": "application/json"},
        )
        _csrf_rejected(res2)
    finally:
        _restore_public_origin(prev)


def test_forged_x_forwarded_proto_does_not_trust_http_public_origin():
    prev = _set_public_origin()
    try:
        c = _proxy_client()
        res = c.post(
            "/api/reset-demo",
            headers={
                "Origin": PUBLIC_HTTP,
                "Content-Type": "application/json",
                "X-Forwarded-Proto": "https",
            },
            json={},
        )
        _csrf_rejected(res)
    finally:
        _restore_public_origin(prev)


def test_public_origin_unset_local_http_same_origin_and_allowlist_still_work():
    prev = os.environ.pop(KEY, None)
    try:
        assert KEY not in os.environ
        a = TestClient(app, raise_server_exceptions=False)
        ok = a.post(
            "/api/cases/empty",
            json={"name": "本机 testserver 同源"},
            headers={"Origin": "http://testserver", "Content-Type": "application/json"},
        )
        assert ok.status_code == 200, ok.text[:400]
        ok2 = a.post(
            "/api/reset-demo",
            headers={"Origin": "http://127.0.0.1:8000", "Content-Type": "application/json"},
            json={},
        )
        assert ok2.status_code == 200, ok2.text[:400]
        ok3 = a.post(
            "/api/cases/empty",
            json={"name": "localhost 白名单"},
            headers={"Origin": "http://localhost:5173", "Content-Type": "application/json"},
        )
        assert ok3.status_code == 200, ok3.text[:400]
    finally:
        _restore_public_origin(prev)
