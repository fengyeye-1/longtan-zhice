"""V8.2.5 超额多情景自检。仅用于隔离测试库。"""
from __future__ import annotations

import json
import sqlite3
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import store


@pytest.fixture(autouse=True)
def isolated_reset():
    store.reset_all_for_tests()


@pytest.fixture
def c():
    return TestClient(app, raise_server_exceptions=False)


def project(c, name="超额企业"):
    r = c.post("/api/cases/empty", json={"name": name})
    assert r.status_code == 200, r.text[:400]
    return r, {
        "X-Session-Id": r.headers["X-Session-Id"],
        "X-Project-Token": r.headers["X-Project-Token"],
    }


def csv_upload(c, h, kind, data):
    return c.post(
        f"/api/upload/commit?kind={kind}",
        headers=h,
        files={"file": ("data.csv", data.encode("utf-8-sig"), "text/csv")},
    )


def persisted_state(case_id):
    with sqlite3.connect(store.DB_PATH) as db:
        row = db.execute("SELECT payload FROM projects WHERE case_id=?", (case_id,)).fetchone()
    return json.loads(row[0]) if row else None


def test_restore_then_edit_then_reopen_sqlite(c):
    r, h = project(c, "恢复再编辑")
    assert c.put("/api/scenario", headers=h, json={"energy_price": 0.9}).status_code == 200
    vid = c.post("/api/versions/save", headers=h, params={"label": "v1"}).json()["version_id"]
    assert c.put("/api/scenario", headers=h, json={"energy_price": 1.3}).status_code == 200
    rr = c.post(f"/api/versions/{vid}/restore", headers=h)
    assert rr.status_code == 200, rr.text[:400]
    assert c.put("/api/scenario", headers=h, json={"energy_price": 0.88}).status_code == 200
    state = c.get("/api/case", headers=h).json()
    db = persisted_state(state["case_id"])
    assert db["scenario"]["energy_price"] == pytest.approx(0.88)
    assert db["revision"] == state["revision"]


def test_ten_thousand_device_ids_unique(c):
    _, h = project(c, "一万设备")
    lines = ["设备名称,设备类型,功率_kW,年运行小时"]
    for i in range(10000):
        lines.append(f"设备{i},球磨机,10,1000")
    r = csv_upload(c, h, "device", "\n".join(lines))
    assert r.status_code == 200, r.text[:400]
    ids = [d["id"] for d in c.get("/api/case", headers=h).json()["devices"]]
    assert len(ids) == 10000
    assert len(set(ids)) == 10000


def test_reimport_preview_mentions_recycle_not_silent_keep(c):
    _, h = project(c)
    rows = "设备名称,设备类型,功率_kW,年运行小时\n磨机A,球磨机,100,4000\n"
    assert csv_upload(c, h, "device", rows).status_code == 200
    prev = c.post(
        "/api/upload/preview?kind=device",
        headers=h,
        files={"file": ("data.csv", rows.encode("utf-8-sig"), "text/csv")},
    )
    assert prev.status_code == 200
    text = prev.json().get("will_replace") or ""
    assert "回收" in text or "不会无声" in text
    assert "技改仍保留但需核对" not in text


def test_two_enterprises_listed_after_subprocess_restart(c):
    a, ha = project(c, "甲企业")
    b = c.post("/api/cases/empty", headers=ha, json={"name": "乙企业"})
    assert b.status_code == 200
    sid = ha["X-Session-Id"]
    tb = b.headers["X-Project-Token"]
    ck_sid, ck_mac = c.cookies.get("ltzs_sid"), c.cookies.get("ltzs_stk")
    code = """from fastapi.testclient import TestClient
from app.main import app
c=TestClient(app)
c.cookies.set("ltzs_sid", %r)
c.cookies.set("ltzs_stk", %r)
h={"X-Session-Id":%r,"X-Project-Token":%r}
r=c.get("/api/cases",headers=h)
print(__import__("json").dumps([x["name"] for x in r.json()["cases"]],ensure_ascii=False))
""" % (ck_sid, ck_mac, sid, tb)
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=25, cwd=os.getcwd())
    assert p.returncode == 0, p.stderr[:400]
    names = json.loads(p.stdout.strip().splitlines()[-1])
    assert "甲企业" in names and "乙企业" in names


def test_energy_csv_rejects_fullwidth_duplicate_year(c):
    _, h = project(c)
    r = csv_upload(c, h, "energy", "年份,工业用电量_万kWh\n2024,100\n２０２４,200\n")
    assert r.status_code in (400, 409, 422)
    years = c.get("/api/case", headers=h).json()["energy_years"]
    assert years == []


def test_energy_csv_javascript_url_sanitized(c):
    _, h = project(c)
    r = csv_upload(
        c, h, "energy",
        "年份,工业用电量_万kWh,来源URL\n2024,100,javascript:alert(1)\n",
    )
    assert r.status_code == 200, r.text[:300]
    years = c.get("/api/case", headers=h).json()["energy_years"]
    assert len(years) == 1
    assert not str(years[0].get("url") or "").lower().startswith("javascript:")


def test_health_version_is_825(c):
    h = c.get("/api/health").json()
    assert h["version"] == "8.2.11"
    assert h["frontend_expected"] == "8.2.11"
    assert h["build_id"]
    assert h["service"]


def test_template_includes_device_id_and_method(c):
    kinds = {k["id"]: k["fields"] for k in c.get("/api/templates").json()["kinds"]}
    assert "设备ID" in kinds["retrofit"]
    assert "计算方法" in kinds["retrofit"]
