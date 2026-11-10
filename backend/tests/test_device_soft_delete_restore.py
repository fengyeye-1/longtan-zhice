# -*- coding: utf-8 -*-
"""设备软删除 / 回收站恢复（含用户自建设备）。"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app
from app.services import store


def setup_function():
    store.reset_all_for_tests()


def _client() -> TestClient:
    return TestClient(app)


def _fork(c: TestClient) -> None:
    r = c.put('/api/scenario', json={'hours': 4000})
    assert r.status_code == 200, r.text


def test_soft_delete_moves_to_removed_and_drops_from_active():
    c = _client()
    _fork(c)
    before = c.get('/api/results').json()['case']
    active_ids = {d['id'] for d in before['devices']}
    assert 'dev-pump' in active_ids
    n_dev = len(before['devices'])
    n_rf = len(before['retrofits'])

    r = c.delete('/api/devices/dev-pump')
    assert r.status_code == 200, r.text
    case = r.json()['case']
    assert len(case['devices']) == n_dev - 1
    assert 'dev-pump' not in {d['id'] for d in case['devices']}
    removed = [d for d in (case.get('removed_devices') or []) if d['id'] == 'dev-pump']
    assert removed
    assert removed[0].get('removed_at')  # 回收站有删除时间
    # 绑定措施也离开 active
    assert 'rf-pump' not in {rf['id'] for rf in case['retrofits']}
    assert any(rf['id'] == 'rf-pump' for rf in case.get('removed_retrofits') or [])
    assert len(case['retrofits']) == n_rf - 1


def test_recycle_bin_lists_soft_deleted_devices():
    c = _client()
    _fork(c)
    assert c.delete('/api/devices/dev-pump').status_code == 200
    r = c.get('/api/devices/recycle-bin')
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['count'] >= 1
    ids = {it['id'] for it in body['items']}
    assert 'dev-pump' in ids
    item = next(it for it in body['items'] if it['id'] == 'dev-pump')
    assert item['name'] == '二级输送泵'
    assert item.get('removed_at')
    assert any(rf['id'] == 'rf-pump' for rf in item.get('bound_retrofits') or [])


def test_restore_device_brings_back_device_and_retrofits():
    c = _client()
    _fork(c)
    assert c.delete('/api/devices/dev-pump').status_code == 200
    r = c.post('/api/devices/dev-pump/restore')
    assert r.status_code == 200, r.text
    case = r.json()['case']
    assert any(d['id'] == 'dev-pump' for d in case['devices'])
    restored = next(d for d in case['devices'] if d['id'] == 'dev-pump')
    assert restored.get('removed_at') in (None, '')
    assert any(rf['id'] == 'rf-pump' for rf in case['retrofits'])
    assert not any(d['id'] == 'dev-pump' for d in case.get('removed_devices') or [])
    assert not any(rf['id'] == 'rf-pump' for rf in case.get('removed_retrofits') or [])
    # 恢复后回收站不再列出
    bin_r = c.get('/api/devices/recycle-bin')
    assert bin_r.status_code == 200
    assert 'dev-pump' not in {it['id'] for it in bin_r.json()['items']}


def test_restore_conflict_when_same_id_already_active():
    c = _client()
    _fork(c)
    assert c.delete('/api/devices/dev-pump').status_code == 200
    assert c.post('/api/devices/dev-pump/restore').status_code == 200
    r = c.post('/api/devices/dev-pump/restore')
    assert r.status_code == 400


def test_user_added_device_soft_delete_and_restore_via_recycle_bin():
    c = _client()
    _fork(c)
    add = c.post('/api/devices', json={
        'name': '用户自建风机',
        'device_type': '风机',
        'power_kw': 80,
        'hours': 3000,
    })
    assert add.status_code == 200, add.text
    new_id = next(d['id'] for d in add.json()['case']['devices'] if d['name'] == '用户自建风机')

    assert c.delete(f'/api/devices/{new_id}').status_code == 200
    case = c.get('/api/results').json()['case']
    assert new_id not in {d['id'] for d in case['devices']}
    assert any(d['id'] == new_id for d in case.get('removed_devices') or [])

    bin_r = c.get('/api/devices/recycle-bin')
    assert any(it['id'] == new_id and it['name'] == '用户自建风机' for it in bin_r.json()['items'])

    r = c.post(f'/api/devices/{new_id}/restore')
    assert r.status_code == 200, r.text
    case = r.json()['case']
    assert any(d['id'] == new_id and d['name'] == '用户自建风机' for d in case['devices'])
    assert not any(d['id'] == new_id for d in case.get('removed_devices') or [])
    assert new_id not in {it['id'] for it in c.get('/api/devices/recycle-bin').json()['items']}


def test_restore_demo_defaults_fills_missing_pump_and_measure():
    """内部兜底：硬删后无回收站条目时，仍可从模板加回（UI 不再暴露）。"""
    c = _client()
    _fork(c)
    del_r = c.delete('/api/devices/dev-pump')
    assert del_r.status_code == 200
    sid = del_r.headers.get('X-Session-Id') or c.cookies.get('ltzs_sid')
    assert sid

    def wipe_bin(case):
        case.removed_devices = [d for d in (case.removed_devices or []) if d.id != 'dev-pump']
        case.removed_retrofits = [rf for rf in (case.removed_retrofits or []) if rf.device_id != 'dev-pump']

    tok = store._ctx_session.set(sid)
    try:
        store.update_case(wipe_bin)
    finally:
        store._ctx_session.reset(tok)

    mid = c.get('/api/results').json()['case']
    assert 'dev-pump' not in {d['id'] for d in mid['devices']}
    assert 'dev-pump' not in {d['id'] for d in mid.get('removed_devices') or []}

    r = c.post('/api/devices/restore-demo-defaults')
    assert r.status_code == 200, r.text
    case = r.json()['case']
    assert any(d['id'] == 'dev-pump' and d['name'] == '二级输送泵' for d in case['devices'])
    assert any(rf['id'] == 'rf-pump' for rf in case['retrofits'])
    assert any(d['id'] == 'dev-mill' for d in case['devices'])
    assert 'dev-pump' in (r.json().get('added_from_template') or [])


def test_restore_demo_defaults_from_bin_prefers_removed():
    c = _client()
    _fork(c)
    assert c.delete('/api/devices/dev-pump').status_code == 200
    r = c.post('/api/devices/restore-demo-defaults')
    assert r.status_code == 200, r.text
    case = r.json()['case']
    assert any(d['id'] == 'dev-pump' for d in case['devices'])
    assert any(rf['id'] == 'rf-pump' for rf in case['retrofits'])
    assert 'dev-pump' in (r.json().get('restored_from_bin') or [])


def test_add_device_succeeds_after_fork():
    c = _client()
    _fork(c)
    r = c.post('/api/devices', json={
        'name': '用户新泵',
        'device_type': '泵',
        'power_kw': 55,
        'hours': 3000,
        'baseline_kwh': 120000,
    })
    assert r.status_code == 200, r.text
    devices = r.json()['case']['devices']
    assert any(d['name'] == '用户新泵' for d in devices)
    ids = {d['id'] for d in devices}
    assert 'dev-mill' in ids and 'dev-pump' in ids


def test_soft_delete_clears_selected_measure_for_removed_device():
    c = _client()
    _fork(c)
    r = c.put('/api/scenario', json={'selected_measure_id': 'rf-pump', 'pump_baseline_kwh': 200000})
    assert r.status_code == 200, r.text
    assert r.json()['results']['case']['scenario']['selected_measure_id'] == 'rf-pump'
    r = c.delete('/api/devices/dev-pump')
    assert r.status_code == 200
    sc = r.json()['case']['scenario']
    assert sc['selected_measure_id'] is None
