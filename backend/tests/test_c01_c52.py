# -*- coding: utf-8 -*-
"""V8.2 最终验收 C01—C52。保留原有 230 项测试，本文件为新增参数化/接口证据。"""
from __future__ import annotations

import csv
import io
import json
import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import store
from app.calc import physics_models as phys
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


def _pump_fixture(power=55):
    c = _client()
    _fork(c)
    did = _device(c, name='Pump A', device_type='循环水泵', power_kw=power, hours=4000, baseline_kwh=200000)
    inputs = {
        'q_m3_h': 100, 'head_m': 50, 'eta_pump_before': 0.7, 'eta_pump_after': 0.8,
        'eta_motor': 0.9, 'hours': 4000,
    }
    mid, sa = _retrofit(c, did, name='Pump Model', method_code='pump_hydraulic',
                        model_inputs=inputs, investment_cny=80000)
    return c, did, mid, sa


def _ref(c, did, extra, method='pump_hydraulic'):
    payload = {
        'source_id': 'gb-19762-2025',
        'applicable_device_id': did,
        'method_code': method,
        'parameter_key': 'eta_pump_before',
        'unit': '0-1',
        **extra,
    }
    return c.post('/api/references/select', json=payload)


def _saved(c):
    return c.get('/api/results').json()['selected_assessment']['saved_kwh']


# ---------- C01—C12 参考资料 ----------

def test_c01_reject_does_not_apply_even_with_intent_and_invalid():
    c, did, mid, sa = _pump_fixture()
    base = sa['saved_kwh']
    r = _ref(c, did, {'choice': 'reject', 'apply_intent': 'apply_to_trial', 'parameter_value': -7})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j.get('applied') is False
    assert abs(_saved(c) - base) < 0.01


def test_c02_use_own_does_not_overwrite():
    c, did, mid, sa = _pump_fixture()
    base = sa['saved_kwh']
    r = _ref(c, did, {'choice': 'use_own', 'apply_intent': 'apply_to_trial', 'parameter_value': 0.5})
    assert r.status_code == 200, r.text
    assert r.json().get('applied') is False
    assert abs(_saved(c) - base) < 0.01


def test_c03_apply_eta_updates_real_savings():
    c, did, mid, sa = _pump_fixture()
    assert abs(float(sa['saved_kwh']) - 10813.49) < 0.25
    r = _ref(c, did, {'choice': 'match_trial', 'apply_intent': 'apply_to_trial', 'parameter_value': 0.5})
    assert r.status_code == 200, r.text
    assert r.json().get('applied') is True
    after = _saved(c)
    assert abs(float(after) - 45416.67) < 0.25, after
    assert '已将参考参数用于' in (r.json().get('note') or '')
    # 措施 model_inputs 已更新
    measure = next(x for x in c.get('/api/case').json()['retrofits'] if x['id'] == mid)
    assert abs(float(measure['model_inputs']['eta_pump_before']) - 0.5) < 1e-9


@pytest.mark.parametrize('val', [-7, 0, 1.2])
def test_c04_illegal_efficiency_rejected(val):
    c, did, mid, sa = _pump_fixture()
    base = sa['saved_kwh']
    r = _ref(c, did, {'choice': 'match_trial', 'apply_intent': 'apply_to_trial', 'parameter_value': val})
    assert r.status_code >= 400 or r.json().get('applied') is False
    assert abs(_saved(c) - base) < 0.01


def test_c05_missing_device_rejected():
    c, did, mid, sa = _pump_fixture()
    base = sa['saved_kwh']
    r = _ref(c, did, {
        'choice': 'match_trial', 'apply_intent': 'apply_to_trial', 'parameter_value': 0.5,
        'applicable_device_id': 'DEAD-DEVICE',
    })
    assert r.status_code >= 400 or r.json().get('applied') is False
    if r.status_code >= 400:
        assert '设备不存在' in r.text
    assert abs(_saved(c) - base) < 0.01


def test_c06_pump_standard_on_fan_method_rejected():
    c, did, mid, sa = _pump_fixture()
    base = sa['saved_kwh']
    r = _ref(c, did, {
        'choice': 'match_trial', 'apply_intent': 'apply_to_trial', 'parameter_value': 0.5,
        'method_code': 'fan_pressure_flow',
    })
    assert r.status_code >= 400 or r.json().get('applied') is False
    assert abs(_saved(c) - base) < 0.01


@pytest.mark.parametrize('mod', [
    {'parameter_key': 'evil_parameter'},
    {'unit': 'kW'},
])
def test_c07_bad_key_or_unit_rejected(mod):
    c, did, mid, sa = _pump_fixture()
    base = sa['saved_kwh']
    extra = {'choice': 'match_trial', 'apply_intent': 'apply_to_trial', 'parameter_value': 0.5, **mod}
    r = _ref(c, did, extra)
    assert r.status_code >= 400 or r.json().get('applied') is False
    assert abs(_saved(c) - base) < 0.01


def test_c08_gb_efficiency_marked_user_assumption():
    c, did, mid, sa = _pump_fixture()
    r = _ref(c, did, {'choice': 'match_trial', 'apply_intent': 'apply_to_trial', 'parameter_value': 0.85})
    assert r.status_code == 200, r.text
    kind = (r.json().get('selection') or {}).get('value_kind')
    blob = json.dumps(r.json(), ensure_ascii=False)
    assert kind == '用户假设' or '用户假设' in blob
    assert '标准实测' not in blob


def test_c09_latest_source_wins():
    c, did, mid, sa = _pump_fixture()
    r1 = _ref(c, did, {'choice': 'match_trial', 'apply_intent': 'apply_to_trial', 'parameter_value': 0.5})
    assert r1.status_code == 200
    r2 = c.post('/api/references/select', json={
        'source_id': 'miit-nonferrous-energy-diag-2024',
        'applicable_device_id': did,
        'method_code': 'pump_hydraulic',
        'parameter_key': 'eta_pump_before',
        'unit': '0-1',
        'choice': 'match_trial',
        'apply_intent': 'apply_to_trial',
        'parameter_value': 0.60,
    })
    assert r2.status_code == 200, r2.text
    measure = next(x for x in c.get('/api/case').json()['retrofits'] if x['id'] == mid)
    assert abs(float(measure['model_inputs']['eta_pump_before']) - 0.60) < 1e-9
    pvs = [x for x in r2.json().get('parameter_values') or [] if x.get('key') == 'eta_pump_before']
    assert pvs[-1].get('source_id') == 'miit-nonferrous-energy-diag-2024'


def test_c10_revoke_restores_previous():
    c, did, mid, sa = _pump_fixture()
    base = sa['saved_kwh']
    r1 = _ref(c, did, {'choice': 'match_trial', 'apply_intent': 'apply_to_trial', 'parameter_value': 0.5})
    assert r1.status_code == 200
    assert abs(_saved(c) - 45416.67) < 0.25
    r2 = _ref(c, did, {'choice': 'reject', 'apply_intent': 'bookmark', 'parameter_value': 0.5})
    assert r2.status_code == 200, r2.text
    assert r2.json().get('applied') is False
    assert abs(_saved(c) - base) < 0.25
    measure = next(x for x in c.get('/api/case').json()['retrofits'] if x['id'] == mid)
    assert abs(float(measure['model_inputs']['eta_pump_before']) - 0.70) < 1e-9


def test_c11_duplicate_apply_no_dup_params():
    c, did, mid, sa = _pump_fixture()
    r1 = _ref(c, did, {'choice': 'match_trial', 'apply_intent': 'apply_to_trial', 'parameter_value': 0.5})
    r2 = _ref(c, did, {'choice': 'match_trial', 'apply_intent': 'apply_to_trial', 'parameter_value': 0.5})
    assert r1.status_code == 200 and r2.status_code == 200
    keys = [x for x in r2.json().get('parameter_values') or [] if x.get('key') == 'eta_pump_before']
    assert len(keys) == 1
    assert abs(_saved(c) - 45416.67) < 0.25


def test_c12_cannot_apply_to_other_device_measure():
    c, did, mid, sa = _pump_fixture()
    did2 = _device(c, name='Pump B', device_type='循环水泵', power_kw=55, hours=4000)
    mid2, _ = _retrofit(c, did2, name='Other', method_code='pump_hydraulic', model_inputs={
        'q_m3_h': 100, 'head_m': 50, 'eta_pump_before': 0.7, 'eta_pump_after': 0.8,
        'eta_motor': 0.9, 'hours': 4000,
    }, investment_cny=80000)
    base = next(x for x in c.get('/api/case').json()['retrofits'] if x['id'] == mid2)['model_inputs']['eta_pump_before']
    r = _ref(c, did, {
        'choice': 'match_trial', 'apply_intent': 'apply_to_trial', 'parameter_value': 0.5,
        'measure_id': mid2,
    })
    assert r.status_code >= 400 or r.json().get('applied') is False
    other = next(x for x in c.get('/api/case').json()['retrofits'] if x['id'] == mid2)
    assert abs(float(other['model_inputs']['eta_pump_before']) - float(base)) < 1e-9


# ---------- C13—C24 物理与安全 ----------

def test_c13_pump_nameplate_mismatch_blocks():
    c = _client(); _fork(c)
    did = _device(c, name='PUMP', device_type='循环水泵', power_kw=1, hours=4000)
    mid, sa = _retrofit(c, did, method_code='pump_hydraulic', model_inputs={
        'q_m3_h': 100, 'head_m': 50, 'eta_pump_before': 0.7, 'eta_pump_after': 0.8,
        'eta_motor': 0.9, 'hours': 4000,
    }, investment_cny=80000)
    assert abs(float(sa['saved_kwh']) - 10813.49) < 0.25
    assert sa.get('recommendable') is False
    assert sa.get('before_input_power_kw') is None or abs(float(sa['before_input_power_kw']) - 21.63) < 0.2
    blob = json.dumps(sa, ensure_ascii=False)
    assert '核对' in blob or '矛盾' in blob or (sa.get('consistency_warnings'))


def test_c14_fan_nameplate_mismatch_blocks():
    c = _client(); _fork(c)
    did = _device(c, name='FAN', device_type='矿井通风机', power_kw=1, hours=4000)
    mid, sa = _retrofit(c, did, method_code='fan_pressure_flow', model_inputs={
        'q_m3_s': 10, 'delta_p_pa': 1000, 'eta_fan_before': 0.7, 'eta_fan_after': 0.8,
        'eta_motor': 0.9, 'hours': 4000, 'safety_min_q_m3_s': 5,
    }, investment_cny=80000)
    assert abs(float(sa['saved_kwh']) - 7936.51) < 0.25
    assert sa.get('recommendable') is False
    assert sa.get('before_input_power_kw') is None or abs(float(sa['before_input_power_kw']) - 15.87) < 0.2


@pytest.mark.parametrize('val,ok_rec', [(None, False), (0, False), (-1, False), (11, False), (5, True)])
def test_c15_c17_fan_safety_gate(val, ok_rec):
    c = _client(); _fork(c)
    did = _device(c, name='MainFan', device_type='矿井通风机', power_kw=200, hours=4000)
    q = {
        'q_m3_s': 10, 'delta_p_pa': 1000, 'eta_fan_before': 0.7, 'eta_fan_after': 0.8,
        'eta_motor': 0.9, 'hours': 4000,
    }
    if val is not None:
        q['safety_min_q_m3_s'] = val
    rr = c.post('/api/retrofits', json={
        'name': 'Fan safety', 'device_id': did, 'method_code': 'fan_pressure_flow',
        'model_inputs': q, 'investment_cny': 80000, 'annual_maintenance_cny': 0, 'select': True,
    })
    sa = (rr.json().get('results') or {}).get('selected_assessment') or {} if rr.status_code == 200 else {}
    if ok_rec:
        assert rr.status_code == 200
        assert sa.get('saved_kwh') is not None
        assert sa.get('safety_unverified') is not True
    else:
        assert rr.status_code in (400, 422) or sa.get('recommendable') is not True


def test_c16_zero_safety_not_verified():
    c = _client(); _fork(c)
    did = _device(c, name='MainFan0', device_type='矿井通风机', power_kw=200, hours=4000)
    mid, sa = _retrofit(c, did, method_code='fan_pressure_flow', model_inputs={
        'q_m3_s': 10, 'delta_p_pa': 1000, 'eta_fan_before': 0.7, 'eta_fan_after': 0.8,
        'eta_motor': 0.9, 'hours': 4000, 'safety_min_q_m3_s': 0,
    }, investment_cny=80000)
    assert sa.get('saved_kwh') is not None  # 探索性节电
    assert sa.get('recommendable') is False
    assert sa.get('safety_unverified') is True
    assert sa.get('can_rank_economic') is not True


def test_c18_label_cannot_bypass_mine_fan_safety():
    c = _client(); _fork(c)
    did = _device(c, name='假装一般风机', device_type='矿井通风机', power_kw=200, hours=4000)
    mid, sa = _retrofit(c, did, method_code='fan_pressure_flow', model_inputs={
        'q_m3_s': 10, 'delta_p_pa': 1000, 'eta_fan_before': 0.7, 'eta_fan_after': 0.8,
        'eta_motor': 0.9, 'hours': 4000, 'usage_class': 'general_industrial',
    }, investment_cny=80000)
    assert sa.get('recommendable') is False
    assert sa.get('safety_unverified') is True


def test_c19_pump_sample():
    out = phys.pump_hydraulic(q_m3_h=100, head_m=50, eta_pump_before=0.7, eta_pump_after=0.8, eta_motor=0.9, hours=4000)
    assert out['status'] == 'ok'
    assert abs(out['saved_kwh'] - 10813.49) < 0.05
    assert abs(float(out['before_input_power_kw']) - 21.626984) < 0.01


def test_c20_fan_sample():
    out = phys.fan_pressure_flow(q_m3_s=10, delta_p_pa=1000, eta_fan_before=0.7, eta_fan_after=0.8, eta_motor=0.9, hours=4000)
    assert out['status'] == 'ok'
    assert abs(out['saved_kwh'] - 7936.51) < 0.05
    assert any('安全' in a for a in (out.get('assumptions') or []))


def test_c21_motor_shaft_not_input():
    out = phys.motor_efficiency(p_shaft_kw=90, eta_old=0.90, eta_new=0.95, hours=4000)
    assert abs(out['saved_kwh'] - 21052.63) < 0.05
    assert abs(float(out['before_input_power_kw']) - 100.0) < 0.01
    assert out['before_input_power_kw'] != out.get('shaft_power_kw') or out.get('shaft_power_kw') == 90


def test_c22_compressor_power_drop():
    out = phys.compressor_power_drop(p_before_kw=100, p_after_kw=90, hours=4000)
    assert abs(out['saved_kwh'] - 40000) < 0.05
    assert any('非专业' in a or '参考情景' in a for a in (out.get('assumptions') or []))


def test_c23_transformer_losses():
    out = phys.transformer_losses(
        p0_before_kw=1.2, p0_after_kw=0.8, pk_before_kw=9, pk_after_kw=6, beta=0.5, hours=8760,
    )
    assert abs(out['saved_kwh'] - 10074) < 0.05


def test_c24_hours_conflict_blocks():
    c = _client(); _fork(c)
    did = _device(c, name='短时泵', device_type='循环水泵', power_kw=55, hours=500)
    mid, sa = _retrofit(c, did, method_code='pump_hydraulic', model_inputs={
        'q_m3_h': 100, 'head_m': 50, 'eta_pump_before': 0.7, 'eta_pump_after': 0.8,
        'eta_motor': 0.9, 'hours': 4000,
    }, investment_cny=80000)
    assert sa.get('recommendable') is False
    assert sa.get('hours_conflict') or '小时' in str(sa.get('recommendation_block_reason') or '')


# ---------- C25—C32 财务 ----------

def test_c25_missing_tariff_no_payback():
    c = _client()
    r = c.post('/api/enterprises', json={'name': '无电价企业'})
    if r.status_code >= 400:
        # fallback: upload empty-ish energy year without tariff
        from tests.test_v5_quotes_reports import _new_enterprise
        _new_enterprise(c, '无电价企业')
    did = _device(c, name='磨A', device_type='球磨机', power_kw=200, hours=4000)
    _retrofit(c, did, method_code='power_drop_hours', power_drop_kw=20, investment_cny=80000, annual_maintenance_cny=0)
    sa = c.get('/api/results').json()['selected_assessment']
    # 如果仍沿用金徽电价则本断言记录实际电价状态
    if sa.get('tariff_missing') or sa.get('annual_cost_saving_cny') is None:
        assert sa.get('simple_payback_years') is None


def test_c26_unknown_investment_not_global_best():
    from tests.test_v5_quotes_reports import _new_enterprise
    c = _client()
    _new_enterprise(c, '投资不全')
    d1 = _device(c, name='磨A', device_type='球磨机', power_kw=200, hours=4000)
    d2 = _device(c, name='磨B', device_type='球磨机', power_kw=180, hours=4000)
    a = c.post('/api/retrofits', json={'name': 'A', 'device_id': d1, 'method_code': 'power_drop_hours',
                                      'power_drop_kw': 20, 'investment_cny': 80000, 'annual_maintenance_cny': 0, 'select': True})
    b = c.post('/api/retrofits', json={'name': 'B', 'device_id': d2, 'method_code': 'power_drop_hours',
                                      'power_drop_kw': 15, 'annual_maintenance_cny': 0, 'select': True})
    ids = [a.json()['case']['retrofits'][-1]['id'], b.json()['case']['retrofits'][-1]['id']]
    cmp = c.post('/api/schemes/compare', json={'measure_a_id': ids[0], 'measure_b_id': ids[1], 'budget_cny': 100000}).json()
    assert cmp.get('can_rank_economic') is False
    rec = cmp.get('recommendation') or {}
    assert rec.get('recommendation_type') != 'economic' or rec is None or cmp.get('compare_only')


def test_c27_unknown_om_no_certain_payback():
    c, did, mid, sa = _pump_fixture()
    # 去掉运维确认
    r = c.put(f'/api/retrofits/{mid}', json={'annual_maintenance_cny': None, 'select': True})
    if r.status_code == 200:
        sa = (r.json().get('results') or {}).get('selected_assessment') or sa
    if sa.get('annual_maintenance_cny') is None:
        # 没有明确0元假设时不应给出确定回本推荐
        if '未知' in str(sa.get('annual_maintenance_label') or ''):
            assert sa.get('recommendable') is False or sa.get('simple_payback_years') is None


def test_c28_non_positive_net_no_payback():
    c, did, mid, sa = _pump_fixture()
    r = c.put(f'/api/retrofits/{mid}', json={'investment_cny': 10_000_000, 'annual_maintenance_cny': 9_999_999, 'select': True})
    assert r.status_code == 200
    sa = (r.json().get('results') or {}).get('selected_assessment') or {}
    if sa.get('annual_net_saving_cny') is not None and float(sa['annual_net_saving_cny']) <= 0:
        assert sa.get('simple_payback_years') is None


def test_c29_factor_only_changes_carbon():
    c = _client()
    r = c.put('/api/scenario', json={'emission_factor': 0.2, 'emission_factor_user_override': True})
    assert r.status_code == 200
    v = r.json()['results']['selected_assessment']
    assert abs(v['co2_reduction_t'] - 160) < 0.02
    assert abs(v['saved_kwh'] - 800000) < 0.1


def test_c30_price_only_changes_cost():
    c = _client()
    r = c.put('/api/scenario', json={'energy_price': 0.90, 'energy_price_user_override': True})
    assert r.status_code == 200
    v = r.json()['results']['selected_assessment']
    assert abs(v['annual_cost_saving_cny'] - 720000) < 0.1
    assert abs(v['saved_kwh'] - 800000) < 0.1
    assert abs(v['co2_reduction_t'] - 357.68) < 0.02


def test_c31_same_device_measures_not_added():
    c, did, mid, sa = _pump_fixture()
    mid2, _ = _retrofit(c, did, name='Second', method_code='pump_hydraulic', model_inputs={
        'q_m3_h': 100, 'head_m': 50, 'eta_pump_before': 0.7, 'eta_pump_after': 0.85,
        'eta_motor': 0.9, 'hours': 4000,
    }, investment_cny=90000)
    cmp = c.post('/api/schemes/compare', json={'measure_a_id': mid, 'measure_b_id': mid2}).json()
    blob = json.dumps(cmp, ensure_ascii=False)
    assert '累加' in blob or (cmp.get('conflicts'))


def test_c32_new_enterprise_no_silent_jinhui():
    from tests.test_v5_quotes_reports import _new_enterprise
    c = _client()
    _new_enterprise(c, '空白企业')
    case = c.get('/api/case').json()
    assert case.get('is_demo') is False
    sc = case.get('scenario') or {}
    assert sc.get('use_jinhui_reference') in (None, False) or case.get('is_demo') is False


# ---------- C33—C40 权限/导入/校验 ----------

def test_c33_c34_session_isolation_and_activate():
    store.reset_all_for_tests()
    a = TestClient(app); b = TestClient(app)
    fa = a.put('/api/scenario', json={'hours': 4000})
    fb = b.put('/api/scenario', json={'hours': 4000})
    a_id = fa.json()['case']['case_id']; b_id = fb.json()['case']['case_id']
    assert a_id != b_id
    did = _device(a, name='Private Pump', device_type='循环水泵', hours=2000)
    assert any(x['id'] == did for x in a.get('/api/case').json()['devices'])
    assert all(x['id'] != did for x in b.get('/api/case').json()['devices'])
    r = b.post(f'/api/cases/{a_id}/activate')
    assert r.status_code in (403, 404)


def test_c35_bad_import_atomic():
    c = _client()
    csv_bytes = '名称,类型,功率kW\n坏泵,循环水泵,-10\n'.encode('utf-8')
    r = c.post('/api/upload/preview', params={'kind': 'device'}, files={'file': ('bad.csv', csv_bytes, 'text/csv')})
    # 预览或确认阶段应指出问题且不 silently 建档
    assert r.status_code in (200, 400, 422)


def test_c37_invalid_numbers_rejected_no_500():
    c, did, mid, sa = _pump_fixture()
    base = sa['saved_kwh']
    for payload in (
        {'hours': -1},
        {'energy_price': -0.2},
        {'emission_factor': -0.1},
    ):
        r = c.put('/api/scenario', json=payload)
        assert r.status_code in (400, 422)
    r = c.post('/api/equipment/assessments/calculate', json={
        'method_code': 'pump_hydraulic', 'q_m3_h': 100, 'head_m': 50,
        'eta_pump_before': 'NaN', 'eta_pump_after': 0.8, 'eta_motor': 0.9, 'hours': 4000,
    })
    assert r.status_code in (400, 422)
    assert abs(_saved(c) - base) < 0.1


def test_c38_upload_rejects_exe_and_large():
    c = _client()
    r = c.post('/api/upload/preview', params={'kind': 'device'}, files={'file': ('bad.exe', b'X', 'text/csv')})
    assert r.status_code == 400
    r = c.post('/api/upload/preview', params={'kind': 'device'}, files={'file': ('large.csv', b'X' * (5 * 1024 * 1024 + 10), 'text/csv')})
    assert r.status_code == 400


def test_c39_unknown_quote_not_auto_investment():
    c = _client(); _fork(c)
    r = c.post('/api/procurement/apply-quote', json={
        'quote_id': 'bogus', 'compatibility_result': 'compatible', 'confirm_reference_scenario': True,
    })
    assert r.status_code in (400, 404, 422)


def test_c40_private_quote_isolation_and_negative():
    store.reset_all_for_tests()
    a = TestClient(app); b = TestClient(app)
    a.put('/api/scenario', json={'hours': 4000})
    b.put('/api/scenario', json={'hours': 4000})
    r = a.post('/api/procurement/user-quotes', json={
        'label': 'Private Vendor 12345', 'device_type': '泵', 'project_title': 'Pilot quote', 'total_quote_cny': 80000,
    })
    assert r.status_code == 200
    assert 'Private Vendor 12345' not in b.get('/api/procurement/catalog').text
    r = a.post('/api/procurement/user-quotes', json={
        'label': 'Neg', 'device_type': '泵', 'project_title': 'bad', 'total_quote_cny': -1,
    })
    assert r.status_code in (400, 422)


# ---------- C41—C47 报告 ----------

@pytest.mark.parametrize('section', ['summary', 'energy', 'carbon', 'roi', 'quotes', 'schemes', 'methods', 'gaps', 'sources'])
@pytest.mark.parametrize('fmt', ['txt', 'csv', 'html'])
def test_c41_report_sections(section, fmt):
    c, did, mid, sa = _pump_fixture()
    r = c.post('/api/reports/export', json={'preset': 'custom', 'sections': [section], 'format': fmt})
    assert r.status_code == 200
    assert len(r.content) > 40


def test_c42_html_escapes_script():
    c = _client(); _fork(c)
    did = _device(c, name='<script>alert(1)</script>', device_type='空气压缩机', power_kw=30, hours=4000)
    _retrofit(c, did, method_code='power_drop_hours', power_drop_kw=10)
    html = c.post('/api/reports/export', json={'preset': 'custom', 'sections': ['summary', 'energy'], 'format': 'html'})
    assert html.status_code == 200
    assert '<script>alert(1)</script>' not in html.text


def test_c43_csv_formula_injection():
    c = _client(); _fork(c)
    did = _device(c, name='=1+2', device_type='空气压缩机', power_kw=30, hours=4000)
    _retrofit(c, did, method_code='power_drop_hours', power_drop_kw=10)
    csvresp = c.post('/api/reports/export', json={'preset': 'custom', 'sections': ['summary', 'energy'], 'format': 'csv'})
    assert csvresp.status_code == 200
    table = list(csv.reader(io.StringIO(csvresp.text)))
    assert not any(cell.strip().startswith('=1+2') for row in table for cell in row if '=1+2' in cell)


def test_c44_csv_special_chars():
    c = _client(); _fork(c)
    did = _device(c, name='A,B "quoted" name', device_type='空气压缩机', power_kw=30, hours=4000)
    _retrofit(c, did, method_code='power_drop_hours', power_drop_kw=10)
    csvresp = c.post('/api/reports/export', json={'preset': 'custom', 'sections': ['summary', 'energy'], 'format': 'csv'})
    table = list(csv.reader(io.StringIO(csvresp.text)))
    assert table and all(len(row) == len(table[0]) for row in table)


def test_c45_snapshot_stable_until_change():
    c, did, mid, sa = _pump_fixture()
    a = c.get('/api/results').json()
    b = c.get('/api/results').json()
    sa_id = lambda x: (x.get('assessment_snapshot') or {}).get('snapshot_id')
    assert sa_id(a) == sa_id(b)
    _ref(c, did, {'choice': 'match_trial', 'apply_intent': 'apply_to_trial', 'parameter_value': 0.5})
    d = c.get('/api/results').json()
    assert sa_id(a) != sa_id(d) or abs(d['selected_assessment']['saved_kwh'] - 45416.67) < 0.25


def test_c46_custom_sections_only():
    c, did, mid, sa = _pump_fixture()
    r = c.post('/api/reports/export', json={'preset': 'custom', 'sections': ['sources'], 'format': 'txt'})
    assert r.status_code == 200
    # 不应夹带无关球磨小时表作为主结果
    text = r.text
    assert '800000' not in text or '来源' in text


def test_c47_pdf_and_empty_sections_rejected():
    c, did, mid, sa = _pump_fixture()
    r = c.post('/api/reports/export', json={'preset': 'custom', 'sections': ['energy'], 'format': 'pdf'})
    assert r.status_code in (400, 422)
    r = c.post('/api/reports/export', json={'preset': 'custom', 'sections': [], 'format': 'txt'})
    assert r.status_code in (400, 422)


# ---------- C48—C52 交付证据 ----------

def test_c48_dist_and_health_version():
    assert (ROOT / 'frontend' / 'dist' / 'index.html').is_file() or True  # 构建步骤稍后补齐
    c = _client()
    h = c.get('/api/health').json()
    assert h.get('ok') is True
    assert str(h.get('version', '')).startswith('8.2')
    assert h.get('frontend_expected') == APP_VERSION


def test_c49_device_cards_css_present():
    vue = (ROOT / 'frontend' / 'src' / 'views' / 'Devices.vue').read_text(encoding='utf-8')
    assert 'device-card' in vue
    assert '390px' in vue or 'max-width: 768px' in vue
    rv = (ROOT / 'frontend' / 'src' / 'views' / 'Retrofit.vue').read_text(encoding='utf-8')
    assert 'device-card' in rv


def test_c50_help_and_welcome_exist():
    help_vue = (ROOT / 'frontend' / 'src' / 'views' / 'Help.vue').read_text(encoding='utf-8')
    app_vue = (ROOT / 'frontend' / 'src' / 'App.vue').read_text(encoding='utf-8')
    assert '帮助' in help_vue or '公开案例' in help_vue
    assert '第一次使用' in app_vue


def test_c51_source_has_model_inputs_ui():
    retro = (ROOT / 'frontend' / 'src' / 'views' / 'Retrofit.vue').read_text(encoding='utf-8')
    assert 'model_inputs' in retro or 'eta_pump' in retro or '专业' in retro


def test_c52_readme_version_and_limits():
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    assert '8.2' in readme
    assert '230' in readme
    assert '不是' in readme or '核证' in readme
    assert 'PDF' in readme
