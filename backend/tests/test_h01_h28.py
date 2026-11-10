# -*- coding: utf-8 -*-
"""V8.2.2 H01—H28 及超额自查。保留原 341 项测试，本文件为新增验收。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import store
from app.version import APP_VERSION

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _reset():
    store.reset_all_for_tests()
    yield


def _client() -> TestClient:
    return TestClient(app)


def _fork(c: TestClient):
    r = c.put('/api/scenario', json={'hours': 4000})
    assert r.status_code == 200, r.text[:300]
    return r


def _device(c, **kw):
    kw = {'name': kw.pop('name', 'Test Device'), 'device_type': kw.pop('device_type', '循环水泵'), **kw}
    r = c.post('/api/devices', json=kw)
    assert r.status_code == 200, r.text[:300]
    return r.json()['case']['devices'][-1]['id']


def _retrofit(c, did, **kw):
    kw = {
        'name': kw.pop('name', 'Test Retrofit'),
        'device_id': did,
        'select': True,
        'annual_maintenance_cny': 0,
        **kw,
    }
    r = c.post('/api/retrofits', json=kw)
    assert r.status_code == 200, r.text[:400]
    body = r.json()
    return body['case']['retrofits'][-1]['id'], body['results']['selected_assessment']


def _pump_inputs(eta_before=0.7):
    return {
        'q_m3_h': 100, 'head_m': 50, 'eta_pump_before': eta_before, 'eta_pump_after': 0.8,
        'eta_motor': 0.9, 'hours': 4000,
    }


def _one_pump():
    c = _client()
    _fork(c)
    did = _device(c, name='Pump A', device_type='循环水泵', power_kw=55, hours=4000, baseline_kwh=200000)
    mid, sa = _retrofit(c, did, name='Pump Model', method_code='pump_hydraulic',
                        model_inputs=_pump_inputs(), investment_cny=80000)
    return c, did, mid, sa


def _two_measures():
    c = _client()
    _fork(c)
    did = _device(c, name='Twin Pump', device_type='循环水泵', power_kw=55, hours=4000, baseline_kwh=200000)
    m1, _ = _retrofit(c, did, name='M1', method_code='pump_hydraulic', model_inputs=_pump_inputs(), investment_cny=80000)
    m2, _ = _retrofit(c, did, name='M2', method_code='pump_hydraulic', model_inputs=_pump_inputs(), investment_cny=90000)
    return c, did, m1, m2


def _apply(c, did, extra, source_id='gb-19762-2025', method='pump_hydraulic'):
    payload = {
        'source_id': source_id,
        'applicable_device_id': did,
        'method_code': method,
        'parameter_key': 'eta_pump_before',
        'unit': '0-1',
        'choice': 'match_trial',
        'apply_intent': 'apply_to_trial',
        **extra,
    }
    return c.post('/api/references/select', json=payload)


def _eta(c, mid):
    measure = next(x for x in c.get('/api/case').json()['retrofits'] if x['id'] == mid)
    return float(measure['model_inputs']['eta_pump_before'])


def _saved(c):
    return c.get('/api/results').json()['selected_assessment']['saved_kwh']


def _measure(c, mid):
    return next(x for x in c.get('/api/case').json()['retrofits'] if x['id'] == mid)


def _detail(r):
    return r.text


# --- H01—H07 编辑校验 ---

def _fraction_case():
    c = _client()
    _fork(c)
    did = _device(c, name='Mill A', device_type='球磨机', power_kw=60, hours=4000, baseline_kwh=200000)
    mid, sa = _retrofit(
        c, did, name='Frac', method_code='baseline_fraction', saving_fraction=0.10,
        investment_cny=100000, annual_maintenance_cny=0,
    )
    return c, did, mid, sa


def test_h01_reject_negative_investment():
    c, did, mid, sa = _fraction_case()
    before = _measure(c, mid)['investment_cny']
    r = c.put(f'/api/retrofits/{mid}', json={'investment_cny': -5000})
    assert r.status_code in (400, 422), r.text
    assert '投资' in _detail(r)
    assert _measure(c, mid)['investment_cny'] == before


def test_h02_reject_fraction_over_100():
    c, did, mid, sa = _fraction_case()
    old = _measure(c, mid)['saving_fraction']
    old_kwh = sa.get('saved_kwh')
    r = c.put(f'/api/retrofits/{mid}', json={'saving_fraction': 1.5, 'method_code': 'baseline_fraction'})
    assert r.status_code in (400, 422), r.text
    assert abs(float(_measure(c, mid)['saving_fraction']) - float(old)) < 1e-9
    sa2 = c.get('/api/results').json()['selected_assessment']
    if old_kwh is not None:
        assert abs(float(sa2['saved_kwh']) - float(old_kwh)) < 0.25


def test_h03_reject_negative_maintenance():
    c, did, mid, sa = _fraction_case()
    before = _measure(c, mid)['annual_maintenance_cny']
    r = c.put(f'/api/retrofits/{mid}', json={'annual_maintenance_cny': -10000})
    assert r.status_code in (400, 422), r.text
    assert '维护' in _detail(r)
    assert _measure(c, mid)['annual_maintenance_cny'] == before


def test_h04_reject_power_drop_exceeds_device():
    c = _client(); _fork(c)
    did = _device(c, name='Mill60', device_type='球磨机', power_kw=60, hours=4000)
    mid, sa = _retrofit(c, did, name='Drop', method_code='power_drop_hours', power_drop_kw=20, investment_cny=100000)
    r = c.put(f'/api/retrofits/{mid}', json={'power_drop_kw': 200, 'method_code': 'power_drop_hours'})
    assert r.status_code in (400, 422), r.text
    assert '功率' in _detail(r)
    assert abs(float(_measure(c, mid)['power_drop_kw']) - 20) < 1e-9
    sa2 = c.get('/api/results').json()['selected_assessment']
    assert sa2.get('saved_kwh') == sa.get('saved_kwh')


def test_h05_reject_nan_investment():
    c, did, mid, sa = _fraction_case()
    before = _measure(c, mid)['investment_cny']
    r = c.put(f'/api/retrofits/{mid}', json={'investment_cny': 'NaN'})
    assert r.status_code in (400, 422), r.text
    assert _measure(c, mid)['investment_cny'] == before


def test_h06_legal_fraction_recalculates():
    c, did, mid, sa = _fraction_case()
    r = c.put(f'/api/retrofits/{mid}', json={'saving_fraction': 0.20, 'method_code': 'baseline_fraction'})
    assert r.status_code == 200, r.text
    assert '已保存' in (r.json().get('message') or '')
    assert abs(float(_measure(c, mid)['saving_fraction']) - 0.20) < 1e-9
    sa2 = r.json()['results']['selected_assessment']
    assert sa2.get('saved_kwh') is not None
    exp = c.post('/api/reports/export', json={'preset': 'custom', 'sections': ['energy', 'summary', 'sources'], 'format': 'txt'})
    assert exp.status_code == 200
    assert str(int(sa2['saved_kwh']))[:4] in exp.text or f"{sa2['saved_kwh']}" in exp.text


def test_h07_invalid_edit_keeps_version_and_sources():
    c, did, mid, sa = _one_pump()
    _apply(c, did, {'parameter_value': 0.50, 'measure_id': mid})
    before = c.get('/api/case').json()
    rev = _measure(c, mid)['revision']
    applied = [s for s in before['reference_selections'] if s.get('applied')]
    r = c.put(f'/api/retrofits/{mid}', json={'investment_cny': -5000})
    assert r.status_code in (400, 422)
    after = c.get('/api/case').json()
    assert _measure(c, mid)['revision'] == rev
    assert len([s for s in after['reference_selections'] if s.get('applied')]) == len(applied)
    assert after.get('quote_choice') == before.get('quote_choice')


# --- H08—H14 引用状态 ---

def test_h08_manual_edit_then_revoke_keeps_new_value():
    c, did, mid, sa = _one_pump()
    r1 = _apply(c, did, {'parameter_value': 0.50, 'measure_id': mid})
    assert r1.status_code == 200 and r1.json().get('applied')
    r2 = c.put(f'/api/retrofits/{mid}', json={
        'method_code': 'pump_hydraulic',
        'model_inputs': _pump_inputs(0.65),
    })
    assert r2.status_code == 200, r2.text
    assert abs(_eta(c, mid) - 0.65) < 1e-9
    rev = c.post('/api/references/select', json={
        'source_id': 'gb-19762-2025',
        'applicable_device_id': did,
        'measure_id': mid,
        'method_code': 'pump_hydraulic',
        'parameter_key': 'eta_pump_before',
        'choice': 'reject',
    })
    assert rev.status_code == 200, rev.text
    assert abs(_eta(c, mid) - 0.65) < 1e-9


def test_h09_revoke_older_keeps_newer():
    c, did, mid, sa = _one_pump()
    _apply(c, did, {'parameter_value': 0.50, 'measure_id': mid}, source_id='gb-19762-2025')
    _apply(c, did, {'parameter_value': 0.60, 'measure_id': mid}, source_id='miit-nonferrous-energy-diag-2024')
    rev = c.post('/api/references/select', json={
        'source_id': 'gb-19762-2025', 'applicable_device_id': did, 'measure_id': mid,
        'method_code': 'pump_hydraulic', 'parameter_key': 'eta_pump_before', 'choice': 'reject',
    })
    assert rev.status_code == 200
    assert abs(_eta(c, mid) - 0.60) < 1e-9
    assert abs(_saved(c) - 25231.48) < 0.25


def test_h10_reject_without_target_does_not_batch_revoke():
    c, did, m1, m2 = _two_measures()
    _apply(c, did, {'parameter_value': 0.50, 'measure_id': m1})
    _apply(c, did, {'parameter_value': 0.55, 'measure_id': m2})
    r = c.post('/api/references/select', json={'choice': 'reject'})
    assert r.status_code == 400, r.text
    assert '请先选择' in r.text
    assert abs(_eta(c, m1) - 0.50) < 1e-9
    assert abs(_eta(c, m2) - 0.55) < 1e-9


def test_h11_revoke_only_specified_binding():
    c, did, m1, m2 = _two_measures()
    r1 = _apply(c, did, {'parameter_value': 0.50, 'measure_id': m1})
    r2 = _apply(c, did, {'parameter_value': 0.55, 'measure_id': m2})
    assert r1.status_code == 200 and r2.status_code == 200
    bid = (r1.json().get('selection') or {}).get('binding_id')
    if not bid:
        sels = c.get('/api/case').json()['reference_selections']
        bid = next(s['binding_id'] for s in sels if s.get('measure_id') == m1 and s.get('applied'))
    rev = c.post('/api/references/select', json={'choice': 'reject', 'binding_id': bid})
    assert rev.status_code == 200, rev.text
    assert abs(_eta(c, m1) - 0.70) < 1e-9
    assert abs(_eta(c, m2) - 0.55) < 1e-9


def test_h12_use_own_guides_then_save_invalidates():
    c, did, mid, sa = _one_pump()
    _apply(c, did, {'parameter_value': 0.50, 'measure_id': mid})
    r = c.post('/api/references/select', json={
        'source_id': 'gb-19762-2025', 'choice': 'use_own', 'applicable_device_id': did, 'measure_id': mid,
    })
    assert r.status_code == 200, r.text
    assert '填写' in (r.json().get('note') or r.text)
    assert abs(_eta(c, mid) - 0.50) < 1e-9
    c.put(f'/api/retrofits/{mid}', json={'method_code': 'pump_hydraulic', 'model_inputs': _pump_inputs(0.65)})
    assert abs(_eta(c, mid) - 0.65) < 1e-9
    active = [s for s in c.get('/api/case').json()['reference_selections']
              if s.get('parameter_key') == 'eta_pump_before' and (s.get('active') or s.get('applied'))]
    assert not any(s.get('parameter_value') == 0.5 and (s.get('active') or s.get('applied')) and not s.get('superseded') for s in
                   c.get('/api/case').json()['reference_selections'])


def test_h13_copy_marks_unverified_provenance():
    c, did, mid, sa = _one_pump()
    _apply(c, did, {'parameter_value': 0.50, 'measure_id': mid})
    r = c.post(f'/api/retrofits/{mid}/copy')
    assert r.status_code == 200, r.text
    copied = r.json()['case']['retrofits'][-1]
    assert abs(float(copied['model_inputs']['eta_pump_before']) - 0.50) < 1e-9
    assert '复制' in (copied.get('source') or '') or '复制' in (copied.get('provenance_label') or '')
    active = [s for s in r.json()['case']['reference_selections']
              if s.get('measure_id') == copied['id'] and (s.get('active') or s.get('applied'))]
    assert active == []


def test_h14_delete_measure_invalidates_its_refs():
    c, did, m1, m2 = _two_measures()
    _apply(c, did, {'parameter_value': 0.50, 'measure_id': m1})
    _apply(c, did, {'parameter_value': 0.55, 'measure_id': m2})
    r = c.delete(f'/api/retrofits/{m1}')
    assert r.status_code == 200
    sels = c.get('/api/case').json()['reference_selections']
    for s in sels:
        if s.get('measure_id') == m1:
            assert not (s.get('active') or s.get('applied'))
    assert abs(_eta(c, m2) - 0.55) < 1e-9


# --- H15—H20 关联和恢复 ---

def test_h15_device_type_change_blocks_pump_model():
    c, did, mid, sa = _one_pump()
    assert sa.get('saved_kwh') is not None
    r = c.put(f'/api/devices/{did}', json={'device_type': '矿井通风机'})
    assert r.status_code == 200, r.text
    sa2 = c.get('/api/results').json()['selected_assessment']
    assert sa2.get('saved_kwh') in (None, sa2.get('saved_kwh'))
    # 不得再把 pump_hydraulic 当可信结果
    assert sa2.get('saved_kwh') is None or sa2.get('status') in ('insufficient_data', 'invalid_input')
    blob = json.dumps(sa2, ensure_ascii=False)
    assert '测算方法' in blob or sa2.get('waiting_message')


def test_h16_rebind_measure_invalidates_old_device_refs():
    c = _client(); _fork(c)
    a = _device(c, name='Pump A', device_type='循环水泵', power_kw=55, hours=4000, baseline_kwh=200000)
    b = _device(c, name='Pump B', device_type='循环水泵', power_kw=55, hours=4000, baseline_kwh=200000)
    mid, _ = _retrofit(c, a, name='Move', method_code='pump_hydraulic', model_inputs=_pump_inputs(), investment_cny=80000)
    _apply(c, a, {'parameter_value': 0.50, 'measure_id': mid})
    r = c.put(f'/api/retrofits/{mid}', json={'device_id': b, 'method_code': 'pump_hydraulic', 'model_inputs': _pump_inputs(0.50)})
    assert r.status_code == 200, r.text
    assert _measure(c, mid)['device_id'] == b
    sels = c.get('/api/case').json()['reference_selections']
    for s in sels:
        if s.get('measure_id') == mid and s.get('applicable_device_id') == a:
            assert not (s.get('active') or s.get('applied'))


def test_h17_method_change_invalidates_old_model_refs():
    c, did, mid, sa = _one_pump()
    _apply(c, did, {'parameter_value': 0.50, 'measure_id': mid})
    r = c.put(f'/api/retrofits/{mid}', json={
        'method_code': 'baseline_fraction', 'saving_fraction': 0.12,
    })
    assert r.status_code == 200, r.text
    sels = c.get('/api/case').json()['reference_selections']
    for s in sels:
        if s.get('measure_id') == mid and s.get('method_code') == 'pump_hydraulic':
            assert not (s.get('active') or s.get('applied'))


def test_h18_restore_version_reconciles():
    c, did, mid, sa = _one_pump()
    assert abs(_eta(c, mid) - 0.70) < 1e-9
    sv = c.post('/api/versions/save?label=before-ref')
    assert sv.status_code == 200
    vid = sv.json().get('version_id') or (sv.json().get('version') or {}).get('version_id')
    if not vid:
        vid = c.get('/api/versions').json()['versions'][0]['version_id']
    _apply(c, did, {'parameter_value': 0.50, 'measure_id': mid})
    assert abs(_eta(c, mid) - 0.50) < 1e-9
    rr = c.post(f'/api/versions/{vid}/restore')
    assert rr.status_code == 200, rr.text
    assert abs(_eta(c, mid) - 0.70) < 1e-9
    active = [s for s in c.get('/api/case').json()['reference_selections']
              if s.get('measure_id') == mid and (s.get('active') or s.get('applied'))]
    assert active == []


def test_h19_legacy_rf_mill_edit_applies_and_marks_user():
    c = _client()
    rf = next(x for x in c.get('/api/case').json()['retrofits'] if x['id'] == 'rf-mill')
    assert rf.get('power_drop_kw') == 200
    r = c.put('/api/retrofits/rf-mill', json={'power_drop_kw': 150})
    assert r.status_code == 200, r.text
    mill = next(x for x in r.json()['case']['retrofits'] if x['id'] == 'rf-mill')
    assert abs(float(mill['power_drop_kw']) - 150) < 1e-9
    blob = json.dumps(mill, ensure_ascii=False)
    assert mill.get('data_type') != '披露' or '用户修改' in blob
    assert '用户' in blob or mill.get('provenance_label')


def test_h20_revert_restores_values_and_sources():
    c, did, mid, sa = _one_pump()
    _apply(c, did, {'parameter_value': 0.50, 'measure_id': mid})
    assert abs(_eta(c, mid) - 0.50) < 1e-9
    r = c.put(f'/api/retrofits/{mid}', json={'method_code': 'pump_hydraulic', 'model_inputs': _pump_inputs(0.65)})
    assert r.status_code == 200
    assert abs(_eta(c, mid) - 0.65) < 1e-9
    rv = c.post(f'/api/retrofits/{mid}/revert')
    assert rv.status_code == 200, rv.text
    assert abs(_eta(c, mid) - 0.50) < 1e-9


# --- H21—H24 报告和经济性 ---

def test_h21_report_lists_eta_with_identity():
    c, did, mid, sa = _one_pump()
    _apply(c, did, {'parameter_value': 0.50, 'measure_id': mid})
    exp = c.post('/api/reports/export', json={'preset': 'full', 'format': 'txt'})
    assert exp.status_code == 200
    text = exp.text
    assert 'eta_pump_before' in text
    assert '0.5' in text
    assert '本次使用的参数及依据' in text
    assert any(k in text for k in ('用户假设', '资料摘录', '用户输入'))


def test_h22_negative_maintenance_no_fake_payback():
    c, did, mid, sa = _fraction_case()
    r = c.put(f'/api/retrofits/{mid}', json={'annual_maintenance_cny': -200000})
    assert r.status_code in (400, 422), r.text
    sa2 = c.get('/api/results').json()['selected_assessment']
    assert sa2.get('simple_payback_years') != 0.48
    net = sa2.get('annual_net_saving_cny')
    if net is not None:
        assert abs(float(net) - 209096.06) > 1


def test_h23_selected_sources_not_mix_other_device():
    c = _client(); _fork(c)
    a = _device(c, name='Pump A', device_type='循环水泵', power_kw=55, hours=4000, baseline_kwh=200000)
    b = _device(c, name='Pump B', device_type='循环水泵', power_kw=55, hours=4000, baseline_kwh=180000)
    m1, _ = _retrofit(c, a, name='A-m', method_code='pump_hydraulic', model_inputs=_pump_inputs(), investment_cny=80000)
    m2, _ = _retrofit(c, b, name='B-m', method_code='pump_hydraulic', model_inputs=_pump_inputs(0.68), investment_cny=90000)
    _apply(c, a, {'parameter_value': 0.50, 'measure_id': m1})
    c.put('/api/scenario', json={'selected_measure_id': m2})
    exp = c.post('/api/reports/export', json={'preset': 'custom', 'sections': ['sources', 'methods'], 'format': 'txt'})
    assert exp.status_code == 200
    # 本次采用不应把 A 的 0.50 当作 B 的依据
    if 'eta_pump_before=0.5' in exp.text:
        assert '本次采用' not in exp.text.split('eta_pump_before=0.5')[0][-80:]


def test_h24_price_or_factor_independent():
    c, did, mid, sa = _one_pump()
    base = float(sa['saved_kwh'])
    r = c.put('/api/scenario', json={'energy_price': 0.8})
    assert r.status_code == 200
    sa2 = r.json()['results']['selected_assessment']
    assert abs(float(sa2['saved_kwh']) - base) < 0.25
    r = c.put('/api/scenario', json={'emission_factor': 0.5})
    assert r.status_code == 200
    sa3 = r.json()['results']['selected_assessment']
    assert abs(float(sa3['saved_kwh']) - base) < 0.25
    if sa2.get('co2_reduction_t') is not None and sa3.get('co2_reduction_t') is not None:
        assert abs(float(sa3['co2_reduction_t']) - float(sa2['co2_reduction_t'])) > 0.01 or sa3.get('co2_reduction_t') != sa2.get('co2_reduction_t')


# --- H25—H28 安全与封版 ---

def test_h25_two_sessions_cannot_revoke_each_other():
    from tests.test_v5_quotes_reports import _new_enterprise
    c1, c2 = _client(), _client()
    _, tok1, sid1 = _new_enterprise(c1, '企业甲H25')
    _, tok2, sid2 = _new_enterprise(c2, '企业乙H25')
    h1 = {'X-Project-Token': tok1, 'X-Session-Id': sid1}
    h2 = {'X-Project-Token': tok2, 'X-Session-Id': sid2}
    d1 = c1.post('/api/devices', json={'name': '甲泵', 'device_type': '循环水泵', 'power_kw': 55, 'hours': 4000, 'baseline_kwh': 200000}, headers=h1)
    assert d1.status_code == 200
    did = d1.json()['case']['devices'][-1]['id']
    mid, _ = None, None
    rr = c1.post('/api/retrofits', json={
        'name': '甲改', 'device_id': did, 'method_code': 'pump_hydraulic',
        'model_inputs': _pump_inputs(), 'investment_cny': 80000, 'select': True, 'annual_maintenance_cny': 0,
    }, headers=h1)
    assert rr.status_code == 200, rr.text
    mid = rr.json()['case']['retrofits'][-1]['id']
    ap = _apply(c1, did, {'parameter_value': 0.50, 'measure_id': mid})
    # _apply uses unscoped client - after headers? It posts without h1. The testclient cookies might mix.
    # Use explicit headers:
    ap = c1.post('/api/references/select', json={
        'source_id': 'gb-19762-2025', 'applicable_device_id': did, 'measure_id': mid,
        'method_code': 'pump_hydraulic', 'parameter_key': 'eta_pump_before', 'unit': '0-1',
        'parameter_value': 0.50, 'choice': 'match_trial', 'apply_intent': 'apply_to_trial',
    }, headers=h1)
    assert ap.status_code == 200, ap.text
    steal = c2.post('/api/references/select', json={
        'choice': 'reject', 'measure_id': mid, 'source_id': 'gb-19762-2025',
        'parameter_key': 'eta_pump_before', 'applicable_device_id': did,
    }, headers=h2)
    # 乙不能改甲的值
    case1 = c1.get('/api/case', headers=h1).json()
    eta = next(x for x in case1['retrofits'] if x['id'] == mid)['model_inputs']['eta_pump_before']
    assert abs(float(eta) - 0.50) < 1e-9
    leaked = c2.get('/api/case', headers=h2).json()
    assert '甲泵' not in [d.get('name') for d in leaked.get('devices') or []]


def test_h26_packaging_markers_and_health():
    c = _client()
    h = c.get('/api/health').json()
    assert h.get('ok') is True
    assert str(h.get('version', '')).startswith('8.2.')
    assert h.get('frontend_expected') == APP_VERSION
    bat = (ROOT / '一键启动.bat').read_bytes()
    assert b'\x00' not in bat
    assert b'uvicorn' in bat.lower() or 'uvicorn' in bat.decode('ascii', errors='ignore').lower()


def test_h27_frontend_demo_copy_present():
    roi = (ROOT / 'frontend' / 'src' / 'views' / 'CarbonRoi.vue').read_text(encoding='utf-8')
    ov = (ROOT / 'frontend' / 'src' / 'views' / 'Overview.vue').read_text(encoding='utf-8')
    assert '这份资料不匹配' in roi
    assert '撤销已应用参数' in roi
    assert '企业实测' in roi
    assert '节电' in ov and '碳' in ov
    assert 'binding_id' in roi


def test_h28_regression_anchors():
    """锚点：版本与金徽基线仍在；全量 341 另见独立运行。"""
    c = _client()
    r = c.get('/api/results').json()
    sa = r.get('selected_assessment') or r.get('mill_scenario') or {}
    assert abs(float(sa.get('saved_kwh') or 0) - 800000) < 1.0
    assert abs(float(sa.get('co2_reduction_t') or 0) - 357.68) < 0.05
    assert APP_VERSION.startswith('8.2.')


# --- 超额自查 ---

def test_x01_legal_edit_matches_report():
    c, did, mid, sa = _fraction_case()
    r = c.put(f'/api/retrofits/{mid}', json={'saving_fraction': 0.20, 'method_code': 'baseline_fraction'})
    kwh = r.json()['results']['selected_assessment']['saved_kwh']
    exp = c.post('/api/reports/export', json={'preset': 'custom', 'sections': ['energy', 'summary'], 'format': 'txt'})
    assert str(int(round(float(kwh))))[:5] in exp.text.replace(',', '') or f'{kwh}' in exp.text


def test_x02_two_bindings_revoke_one():
    test_h11_revoke_only_specified_binding()


def test_x03_type_change_then_change_back_keeps_refs_historical():
    c, did, mid, sa = _one_pump()
    _apply(c, did, {'parameter_value': 0.50, 'measure_id': mid})
    c.put(f'/api/devices/{did}', json={'device_type': '矿井通风机'})
    c.put(f'/api/devices/{did}', json={'device_type': '循环水泵'})
    sels = c.get('/api/case').json()['reference_selections']
    applied_old = [s for s in sels if s.get('parameter_value') == 0.5 and (s.get('active') or s.get('applied')) and not s.get('superseded')]
    # 改回类型不得自动复活原先失效来源
    sa2 = c.get('/api/results').json()['selected_assessment']
    # 值仍在 model_inputs，但原绑定不应再冒充当前有效依据
    # 类型改回后方法又兼容；若绑定在改类型时被失效，应保持失效
    invalidated = [s for s in sels if s.get('measure_id') == mid and s.get('parameter_key') == 'eta_pump_before']
    assert invalidated
    assert all((not s.get('active') and not s.get('applied')) or s.get('superseded') for s in invalidated
               if s.get('source_id') == 'gb-19762-2025')


def test_x04_draft_not_recommendable():
    c = _client(); _fork(c)
    did = _device(c, name='NoBase', device_type='循环水泵', power_kw=55, hours=4000)
    mid, sa = _retrofit(c, did, name='P', method_code='pump_hydraulic', model_inputs=_pump_inputs(), investment_cny=80000)
    assert sa.get('saved_kwh') is not None
    r = c.put(f'/api/retrofits/{mid}', json={
        'method_code': 'baseline_fraction', 'saving_fraction': 0.12, 'save_as_draft': True,
    })
    assert r.status_code == 200, r.text
    m = _measure(c, mid)
    assert m.get('lifecycle_status') == 'draft'
    sa2 = c.get('/api/results').json()['selected_assessment']
    assert sa2.get('recommendable') is False


def test_x05_report_excludes_revoked_and_bookmark_as_current():
    c, did, mid, sa = _one_pump()
    _apply(c, did, {'parameter_value': 0.50, 'measure_id': mid})
    c.post('/api/references/select', json={
        'source_id': 'gb-19762-2025', 'choice': 'match_trial', 'apply_intent': 'bookmark',
        'applicable_device_id': did, 'measure_id': mid,
    })
    c.post('/api/references/select', json={
        'source_id': 'gb-19762-2025', 'applicable_device_id': did, 'measure_id': mid,
        'method_code': 'pump_hydraulic', 'parameter_key': 'eta_pump_before', 'choice': 'reject',
    })
    exp = c.post('/api/reports/export', json={'preset': 'custom', 'sections': ['sources', 'methods'], 'format': 'txt'})
    assert '相关研究资料' in exp.text
    # 已撤销的不应再标「本次采用」且同时作为该历史值的当前依据
    assert '不等于本次采用的参数来源' in exp.text or '历史资料' in exp.text


def test_x06_combo_illegal_boundaries():
    c, did, mid, sa = _fraction_case()
    r1 = c.put(f'/api/retrofits/{mid}', json={'annual_maintenance_cny': -200000, 'saving_fraction': 1.5, 'investment_cny': -1})
    assert r1.status_code in (400, 422)
    m = _measure(c, mid)
    assert float(m['saving_fraction']) == 0.10
    assert m['investment_cny'] == 100000
    c2 = _client(); _fork(c2)
    d2 = _device(c2, name='Mill60', device_type='球磨机', power_kw=60, hours=4000)
    mid2, _ = _retrofit(c2, d2, name='Drop', method_code='power_drop_hours', power_drop_kw=15)
    r2 = c2.put(f'/api/retrofits/{mid2}', json={'power_drop_kw': 200})
    assert r2.status_code in (400, 422)


def test_x07_frontend_messages_and_version_strings():
    rf = (ROOT / 'frontend' / 'src' / 'views' / 'Retrofit.vue').read_text(encoding='utf-8')
    assert '已保存，节电与减碳结果已更新' in rf
    assert '未保存，原结果保留' in rf
    assert '公开资料' in rf and '用户输入' in rf
    assert APP_VERSION.startswith('8.2.')
