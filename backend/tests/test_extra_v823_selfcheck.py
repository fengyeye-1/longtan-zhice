"""V8.2.3 超额自检：比任务书更多情景。仅用于隔离测试库。"""
from __future__ import annotations

import csv
import io
import math
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import store


@pytest.fixture(autouse=True)
def reset_store():
    store.reset_all_for_tests()


@pytest.fixture
def c():
    return TestClient(app, raise_server_exceptions=False)


def csv_data(columns, rows):
    b = io.StringIO()
    w = csv.writer(b)
    w.writerow(columns)
    w.writerows(rows)
    return b.getvalue().encode('utf-8-sig')


def upload(c, kind, columns, rows):
    return c.post(
        '/api/upload/commit',
        params={'kind': kind},
        files={'file': (kind + '.csv', csv_data(columns, rows), 'text/csv')},
    )


def fork(c):
    r = c.put('/api/scenario', json={'hours': 4000})
    assert r.status_code == 200, r.text[:300]
    return r


def test_missing_vs_explicit_zero_vs_negative_maintenance(c):
    fork(c)
    assert upload(c, 'device',
                  ['设备名称', '设备类型', '功率_kW', '年运行小时'],
                  [['磨机A', '球磨机', 100, 4000]]).status_code == 200
    assert c.put('/api/scenario', json={'hours': 4000, 'energy_price': 0.8}).status_code == 200

    missing = upload(c, 'retrofit',
                     ['项目名称', '目标设备', '功率降低_kW', '投资_元'],
                     [['缺维护', '磨机A', 20, 100000]])
    assert missing.status_code == 200, missing.text
    rf = c.get('/api/case').json()['retrofits'][0]
    assert rf['annual_maintenance_cny'] is None
    assert c.get('/api/results').json()['selected_assessment']['simple_payback_years'] is None

    zero = upload(c, 'retrofit',
                  ['项目名称', '目标设备', '功率降低_kW', '投资_元', '年维护费_元'],
                  [['显式零', '磨机A', 20, 100000, 0]])
    assert zero.status_code == 200, zero.text
    rf = c.get('/api/case').json()['retrofits'][0]
    assert rf['annual_maintenance_cny'] == 0
    pb = c.get('/api/results').json()['selected_assessment']['simple_payback_years']
    assert pb == pytest.approx(1.56, abs=0.01)

    neg = upload(c, 'retrofit',
                 ['项目名称', '目标设备', '功率降低_kW', '投资_元', '年维护费_元'],
                 [['负维护', '磨机A', 20, 100000, -1]])
    assert neg.status_code in (400, 422)
    assert c.get('/api/case').json()['retrofits'][0]['name'] == '显式零'


def test_jinhui_power_200_150_restore(c):
    r = c.put('/api/scenario', json={'hours': 4000, 'power_drop_kw': 150, 'selected_measure_id': 'rf-mill'})
    assert r.status_code == 200
    selected = next(x for x in c.get('/api/case').json()['retrofits'] if x['id'] == 'rf-mill')
    assert selected['power_drop_kw'] == pytest.approx(150)
    assert c.get('/api/results').json()['selected_assessment']['saved_kwh'] == pytest.approx(600000)

    r = c.put('/api/scenario', json={'hours': 4000, 'power_drop_kw': 200, 'selected_measure_id': 'rf-mill'})
    assert r.status_code == 200
    selected = next(x for x in c.get('/api/case').json()['retrofits'] if x['id'] == 'rf-mill')
    assert selected['power_drop_kw'] == pytest.approx(200)
    assert c.get('/api/results').json()['selected_assessment']['saved_kwh'] == pytest.approx(800000)


@pytest.mark.parametrize('raw', [
    b'{"energy_price": 1e308}',
    b'{"emission_factor": "NaN"}',
    b'{"energy_price": "Infinity"}',
])
def test_nan_inf_huge_scenario_does_not_pollute(c, raw):
    orig = fork(c).json()['case']['scenario']
    r = c.put('/api/scenario', content=raw, headers={'Content-Type': 'application/json'})
    assert r.status_code in (400, 422), r.text[:200]
    current = c.get('/api/case').json()['scenario']
    assert current['energy_price'] == orig['energy_price']
    assert current['emission_factor'] == orig['emission_factor']
    assert c.get('/api/results').status_code == 200


def test_use_own_wrong_and_correct_unit(c):
    fork(c)
    dev = c.post('/api/devices', json={
        'name': 'Pump A', 'device_type': '循环水泵', 'power_kw': 55, 'hours': 4000, 'baseline_kwh': 200000,
    })
    did = dev.json()['case']['devices'][-1]['id']
    measure = c.post('/api/retrofits', json={
        'name': 'Pump Retrofit', 'device_id': did, 'method_code': 'pump_hydraulic',
        'model_inputs': {
            'q_m3_h': 100, 'head_m': 50, 'eta_pump_before': 0.7,
            'eta_pump_after': 0.8, 'eta_motor': 0.9, 'hours': 4000,
        },
        'investment_cny': 50000, 'annual_maintenance_cny': 0,
    })
    mid = measure.json()['case']['retrofits'][-1]['id']
    wrong = c.post('/api/references/select', json={
        'source_id': 'gb-19762-2025', 'applicable_device_id': did,
        'method_code': 'pump_hydraulic', 'parameter_key': 'q_m3_h',
        'unit': 'm3/s', 'choice': 'use_own', 'parameter_value': 200, 'measure_id': mid,
    })
    assert wrong.status_code in (400, 422)
    q = next(x for x in c.get('/api/case').json()['retrofits'] if x['id'] == mid)['model_inputs']['q_m3_h']
    assert q == 100
    ok = c.post('/api/references/select', json={
        'source_id': 'gb-19762-2025', 'applicable_device_id': did,
        'method_code': 'pump_hydraulic', 'parameter_key': 'q_m3_h',
        'unit': 'm3/h', 'choice': 'use_own', 'parameter_value': 120, 'measure_id': mid,
    })
    assert ok.status_code == 200, ok.text[:300]
    q = next(x for x in c.get('/api/case').json()['retrofits'] if x['id'] == mid)['model_inputs']['q_m3_h']
    assert q == pytest.approx(120)


def test_blank_device_and_retrofit_names_manual_and_csv(c):
    fork(c)
    assert c.post('/api/devices', json={'name': '', 'device_type': '球磨机', 'power_kw': 10, 'hours': 1000}).status_code in (400, 422)
    assert c.post('/api/devices', json={'name': '\t', 'device_type': '球磨机', 'power_kw': 10, 'hours': 1000}).status_code in (400, 422)
    assert upload(c, 'device', ['设备名称', '设备类型', '功率_kW', '年运行小时'],
                  [['', '球磨机', 10, 1000]]).status_code in (400, 422)
    dev0 = c.get('/api/case').json()['devices'][0]
    assert c.post('/api/retrofits', json={
        'name': '  ', 'device_id': dev0['id'], 'power_drop_kw': 1, 'investment_cny': 100, 'annual_maintenance_cny': 0,
    }).status_code in (400, 422)
    assert upload(c, 'retrofit', ['项目名称', '目标设备', '功率降低_kW', '投资_元', '年维护费_元'],
                  [['', '选厂球磨机', 10, 2000, 0]]).status_code in (400, 422)


def test_three_version_saves_same_second_unique(c):
    fork(c)
    ids = []
    for i in range(3):
        r = c.post('/api/versions/save', params={'label': f'v{i}'})
        assert r.status_code == 200, r.text
        ids.append(r.json()['version_id'])
    assert len(set(ids)) == 3


def test_empty_enterprise_has_no_demo_history(c):
    r = c.post('/api/cases/empty', params={'name': '空白矿'})
    assert r.status_code == 200, r.text
    case = r.json()['case']
    assert case['is_demo'] is False
    assert case['devices'] == []
    assert case['retrofits'] == []
    assert case['energy_years'] == []
    assert case['name'] == '空白矿'


def test_mixed_concurrent_device_and_retrofit_writes(c):
    fork(c)
    sid = c.get('/api/case').headers.get('X-Session-Id') or fork(c).headers['X-Session-Id']
    tok = c.get('/api/case').headers.get('X-Project-Token')
    # ensure token from last write
    created = c.post('/api/devices', json={'name': '基线设备', 'device_type': '球磨机', 'power_kw': 40, 'hours': 1000})
    assert created.status_code == 200
    sid = created.headers['X-Session-Id']
    tok = created.headers['X-Project-Token']
    base_id = created.json()['case']['devices'][-1]['id']
    headers = {'X-Session-Id': sid, 'X-Project-Token': tok}

    def job(i):
        with TestClient(app, raise_server_exceptions=False) as sub:
            if i % 2 == 0:
                r = sub.post('/api/devices', json={
                    'name': f'混合设备_{i}', 'device_type': '球磨机', 'power_kw': 20, 'hours': 1000,
                }, headers=headers)
            else:
                r = sub.post('/api/retrofits', json={
                    'name': f'混合措施_{i}', 'device_id': base_id, 'power_drop_kw': 2,
                    'investment_cny': 1000, 'annual_maintenance_cny': 0,
                }, headers=headers)
            return i, r.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        out = list(pool.map(job, range(20)))
    assert all(st in (200, 409) for _, st in out), out
    case = c.get('/api/case', headers=headers).json()
    ok_dev = {f'混合设备_{i}' for i, st in out if st == 200 and i % 2 == 0}
    ok_rf = {f'混合措施_{i}' for i, st in out if st == 200 and i % 2 == 1}
    names_d = {d['name'] for d in case['devices']}
    names_r = {r['name'] for r in case['retrofits']}
    assert ok_dev <= names_d
    assert ok_rf <= names_r
