"""将测试存储隔离到临时目录，禁止 reset_all_for_tests() 触碰真实企业库。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="ltzs_pytest_"))
os.environ["LTZS_DATA_DIR"] = str(_TEST_ROOT)


def _redirect_store():
    from app.services import store

    store.ROOT = _TEST_ROOT
    store.DB_PATH = _TEST_ROOT / "projects.sqlite"
    store.VERSIONS = _TEST_ROOT / "versions"
    store.VERSIONS.mkdir(parents=True, exist_ok=True)
    store.SESSION_SECRET_PATH = _TEST_ROOT / ".session_hmac"
    store._SESSION_SECRET = None
    store._SESSIONS.clear()
    store._CASES.clear()
    store._TOKENS.clear()
    store._REPORT_ARCHIVES.clear()
    store.init_store()
    return _TEST_ROOT


TEST_DATA_ROOT = _redirect_store()


@pytest.fixture(scope="session", autouse=True)
def _isolated_sqlite_session():
    from app.services import store

    assert str(store.DB_PATH).startswith(str(TEST_DATA_ROOT))
    yield
