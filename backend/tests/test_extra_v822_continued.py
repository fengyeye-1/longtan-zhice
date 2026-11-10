"""
陇碳智策 V8.2.2：续审专项回归测试
！！！危险：测试会调用 reset_all_for_tests() 删除测试库！！！
只允许在原ZIP的独立临时解压副本运行，绝不能放在实际企业生产数据库目录执行。

建议执行：
    cd <独立解压目录>/backend
    python -m pytest -q tests/test_extra_v822_continued.py

本文件在原始 V8.2.2 上预计失败；用于验证 Grok 修复后的行为。
"""
from __future__ import annotations

import csv
import io
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.schema import Device
from app.services import store


@pytest.fixture(autouse=True)
def only_test_database():
    store.reset_all_for_tests()


@pytest.fixture
def c():
    return TestClient(app, raise_server_exceptions=False)


def new_editable(c):
    r = c.put("/api/scenario", json={"hours": 4000})
    assert r.status_code == 200, r.text[:200]
    return (
        r.json()["case"]["case_id"],
        r.headers["X-Session-Id"],
        r.headers["X-Project-Token"],
    )


def csv_data(columns, rows):
    b = io.StringIO()
    w = csv.writer(b)
    w.writerow(columns)
    w.writerows(rows)
    return b.getvalue().encode("utf-8-sig")


def upload(c, kind, columns, rows):
    return c.post(
        "/api/upload/commit",
        params={"kind": kind},
        files={"file": (kind + ".csv", csv_data(columns, rows), "text/csv")},
    )


def test_request_context_uses_own_token_not_other_tabs_active_case(c):
    """两个请求上下文用同一会话但不同合法项目Token，必须各自操作明确的项目。"""
    ca, sid, ta = new_editable(c)
    assert c.post("/api/reset-demo").status_code == 200
    cb, sidb, tb = new_editable(c)
    assert ca != cb and sid == sidb and ta != tb

    context_a, context_b = Context(), Context()
    context_a.run(store.set_request_context, sid, ta)
    context_b.run(store.set_request_context, sid, tb)

    # 旧版：context_a会错误读取B项目；无需线程也可确定性复现。
    assert context_a.run(store.get_case).case_id == ca, "Token A不应被共享active_case切换到B"
    assert context_b.run(store.get_case).case_id == cb


def test_two_stale_drafts_cannot_both_succeed_and_lose_first_change(c):
    """两个草稿来自同一旧版本：须合并有效变更或明确409冲突，不能静默覆盖。"""
    cid, sid, tok = new_editable(c)
    context = Context()
    context.run(store.set_request_context, sid, tok)
    a, b = context.run(store.get_case), context.run(store.get_case)
    a.devices.append(Device(id="audit-dev-A", name="并发A", device_type="球磨机", power_kw=40, hours=1000))
    b.devices.append(Device(id="audit-dev-B", name="并发B", device_type="球磨机", power_kw=40, hours=1000))

    context.run(store.commit_case, a)
    second_accepted = True
    try:
        context.run(store.commit_case, b)
    except Exception as exc:
        # 并发冲突应明确报告，不允许把任意500/存储故障当作合格处理
        msg = str(exc).lower()
        status = getattr(exc, "status_code", None)
        assert status == 409 or "conflict" in msg or "冲突" in msg or "版本已变化" in msg, (
            f"第二个草稿失败的原因不是可解释的版本冲突：{exc!r}"
        )
        second_accepted = False

    active = context.run(store.get_case)
    ids = {d.id for d in active.devices}
    assert "audit-dev-A" in ids, "先保存成功的设备不能被另一个旧草稿悄悄抹掉"
    if second_accepted:
        assert "audit-dev-B" in ids, "若第二次保存也宣称成功，它的设备必须保留"


def test_report_snapshot_changes_when_report_content_changes(c):
    """改名不改节电量也会改变报告正文，快照指纹应能区分。"""
    new_editable(c)
    old = c.get("/api/results").json()
    old_id = old["assessment_snapshot"]["snapshot_id"]
    old_kwh = old["selected_assessment"]["saved_kwh"]
    r = c.put("/api/retrofits/rf-mill", json={"name": "球磨机改造方案_新名称"})
    assert r.status_code == 200, r.text[:250]
    new = c.get("/api/results").json()
    assert old_kwh == new["selected_assessment"]["saved_kwh"]
    assert new["selected_assessment"]["measure_name"] == "球磨机改造方案_新名称"
    assert old_id != new["assessment_snapshot"]["snapshot_id"], (
        "报告措施名称已经变化，但两个不同报告仍使用相同快照ID"
    )


def test_report_snapshot_ids_do_not_collide_in_archive_lookup(c):
    """两份不同报告的snapshot ID不能重合，避免按快照ID只找回旧报告。"""
    new_editable(c)
    old = c.post("/api/reports/export", json={"preset": "full", "format": "txt"})
    assert old.status_code == 200
    r = c.put("/api/retrofits/rf-mill", json={"name": "与原报告不同的名称"})
    assert r.status_code == 200
    new = c.post("/api/reports/export", json={"preset": "full", "format": "txt"})
    assert new.status_code == 200
    assert new.text != old.text
    rows = c.get("/api/reports/archives").json()["reports"]
    assert len(rows) == 2
    assert rows[0]["snapshot_id"] != rows[1]["snapshot_id"], (
        "不同正文的报告不应共用一个快照ID"
    )
    for item in rows:
        result = c.get("/api/reports/archives/" + item["snapshot_id"])
        assert result.status_code == 200
        assert result.json()["text"] == item["text"]


@pytest.mark.parametrize("name", ["", "   ", "\t"])
def test_manual_retrofit_requires_nonblank_name(c, name):
    new_editable(c)
    dev = c.get("/api/case").json()["devices"][0]
    before = len(c.get("/api/case").json()["retrofits"])
    r = c.post("/api/retrofits", json={
        "name": name,
        "device_id": dev["id"],
        "power_drop_kw": 10,
        "investment_cny": 2000,
        "annual_maintenance_cny": 0,
    })
    assert r.status_code in (400, 422), r.text[:200]
    assert len(c.get("/api/case").json()["retrofits"]) == before


@pytest.mark.parametrize("name", ["", "   "])
def test_imported_retrofit_requires_nonblank_name(c, name):
    new_editable(c)
    before = c.get("/api/case").json()["retrofits"]
    r = upload(
        c, "retrofit",
        ["项目名称", "目标设备", "功率降低_kW", "投资_元", "年维护费_元"],
        [[name, "选厂球磨机", 10, 2000, 0]],
    )
    assert r.status_code in (400, 422), r.text[:200]
    assert c.get("/api/case").json()["retrofits"] == before, "非法导入不可替换旧数据"


def test_parallel_successful_adds_cannot_go_missing(c):
    """真实并发API：每一条成功200的新增设备最后都必须存在。"""
    _, sid, tok = new_editable(c)
    base_ids = {d["id"] for d in c.get("/api/case").json()["devices"]}
    names = [f"审计并发_{i}" for i in range(30)]
    headers = {"X-Session-Id": sid, "X-Project-Token": tok}

    def add(name):
        with TestClient(app, raise_server_exceptions=False) as sub:
            r = sub.post("/api/devices", json={
                "name": name, "device_type": "球磨机", "power_kw": 30, "hours": 1000,
            }, headers=headers)
            return name, r.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(add, names))
    assert all(status in (200, 409) for _, status in responses), responses
    finally_saved = {d["name"] for d in c.get("/api/case").json()["devices"]}
    successful = {name for name, status in responses if status == 200}
    assert successful <= finally_saved, (
        f"并发接口声称成功的设备有丢失：{sorted(successful - finally_saved)}"
    )


def test_parallel_two_projects_never_write_to_wrong_case(c):
    """同会话中两个合法项目的并发写入不得相互污染。"""
    c.post("/api/reset-demo")
    a = upload(c, "device", ["设备名称", "设备类型", "功率_kW", "年运行小时"],
               [["原有_A", "球磨机", 100, 4000]])
    assert a.status_code == 200, a.text[:200]
    ca, ta, sid = a.json()["case_id"], a.headers["X-Project-Token"], a.headers["X-Session-Id"]
    c.post("/api/reset-demo")
    b = upload(c, "device", ["设备名称", "设备类型", "功率_kW", "年运行小时"],
               [["原有_B", "球磨机", 100, 4000]])
    assert b.status_code == 200, b.text[:200]
    cb, tb = b.json()["case_id"], b.headers["X-Project-Token"]
    assert ca != cb

    def add(i):
        target_id, token = (ca, ta) if i % 2 == 0 else (cb, tb)
        with TestClient(app, raise_server_exceptions=False) as sub:
            r = sub.post("/api/devices", json={
                "name": ("A_" if i % 2 == 0 else "B_") + str(i),
                "device_type": "球磨机", "power_kw": 40, "hours": 1000,
            }, headers={"X-Session-Id": sid, "X-Project-Token": token})
            actual_id = r.json().get("case", {}).get("case_id") if r.status_code == 200 else None
            return i, target_id, actual_id, r.status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(add, range(30)))
    for i, target, actual, status in outcomes:
        assert status in (200, 409), (i, status)
        if status == 200:
            assert actual == target, f"请求{i}期望写入{target}，实际写入{actual}"

    for target, token, should, must_not in (
        (ca, ta, "A_", "B_"), (cb, tb, "B_", "A_")
    ):
        r = c.get("/api/case", headers={
            "X-Session-Id": sid, "X-Project-Token": token,
        })
        assert r.status_code == 200
        assert r.json()["case_id"] == target
        assert not any(d["name"].startswith(must_not) for d in r.json()["devices"]), (
            f"{target}里混入了其他项目的设备"
        )
