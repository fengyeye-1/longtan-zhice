# V8.2.2 further independent regressions — ONLY run in isolated audit copy.
from __future__ import annotations

import csv
import io
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.services import store


@pytest.fixture(autouse=True)
def isolated_reset():
    # DANGER: This deletes the database of the extracted audit copy.
    store.reset_all_for_tests()


@pytest.fixture
def c():
    return TestClient(app, raise_server_exceptions=False)


def csv_upload(c, kind, headers, rows):
    text = io.StringIO()
    writer = csv.writer(text)
    writer.writerow(headers)
    writer.writerows(rows)
    return c.post('/api/upload/commit', params={'kind': kind}, files={
        'file': (f'{kind}.csv', text.getvalue().encode('utf-8-sig'), 'text/csv')
    })


def fork(c):
    r = c.put('/api/scenario', json={'hours': 4000})
    assert r.status_code == 200, r.text[:500]
    return c.get('/api/case').json()


@pytest.mark.parametrize('field', ['energy_price', 'emission_factor'])
def test_huge_finite_scenario_inputs_reject_without_corrupting_persisted_project(c, field):
    original = fork(c)
    wrong = c.put('/api/scenario', json={field: 1e308})
    assert wrong.status_code in (400, 422), f'{field}: 必须明确拒绝，而不是500: {wrong.text[:200]}'
    current = c.get('/api/case').json()
    assert current['scenario'][field] == original['scenario'][field], '拒绝失败后不得污染持久化项目'
    assert c.get('/api/results').status_code == 200, '一次错误输入后原结果仍必须可用'


def test_huge_finite_device_power_reject_without_breaking_project(c):
    original = fork(c)
    wrong = c.post('/api/devices', json={
        'name': '超大功率', 'device_type': '球磨机',
        'power_kw': 1e308, 'hours': 6000,
    })
    assert wrong.status_code in (400, 422), wrong.text[:200]
    current = c.get('/api/case').json()
    assert len(current['devices']) == len(original['devices'])
    assert c.get('/api/results').status_code == 200


def test_huge_finite_csv_power_reject_without_breaking_project(c):
    original = fork(c)
    r = csv_upload(c, 'device',
        ['设备名称', '设备类型', '功率_kW', '年运行小时'],
        [['荒谬大功率', '球磨机', 1e308, 4000]])
    assert r.status_code in (400, 422), r.text[:200]
    assert c.get('/api/case').json()['devices'] == original['devices']
    assert c.get('/api/results').status_code == 200


@pytest.mark.parametrize('source_id', [[1,2], {'foo':'bar'}])
def test_reference_source_id_wrong_json_type_returns_400_not_500(c, source_id):
    r = c.post('/api/references/select', json={'source_id': source_id, 'choice': 'reject'})
    assert r.status_code in (400, 422), r.text[:200]


@pytest.mark.parametrize('method_code', [[1,2], {'foo':'bar'}])
def test_equipment_method_code_wrong_json_type_returns_400_not_500(c, method_code):
    r = c.post('/api/equipment/assessments/calculate', json={'method_code': method_code})
    assert r.status_code in (400, 422), r.text[:200]


@pytest.mark.parametrize('name', ['', '   '])
def test_blank_device_name_on_csv_upload_rejected(c, name):
    r = csv_upload(c, 'device',
        ['设备名称', '设备类型', '功率_kW', '年运行小时'],
        [[name, '球磨机', 100, 4000]])
    assert r.status_code in (400, 422), '空设备名不应成功或被变成nan'
    case = c.get('/api/case').json()
    assert all((d['name'] or '').strip().lower() != 'nan' for d in case['devices'])


def test_blank_device_name_on_manual_create_rejected(c):
    fork(c)
    r = c.post('/api/devices', json={'name': '   ', 'device_type': '球磨机', 'power_kw': 100, 'hours': 4000})
    assert r.status_code in (400, 422), r.text[:200]


def test_fractional_energy_year_is_not_silently_truncated(c):
    fork(c)
    r = csv_upload(c, 'energy',
        ['年份', '工业用电量_万kWh'],
        [[2024.7, 100]])
    assert r.status_code in (400, 422), '2024.7不应静默转换为2024'


def test_zero_emission_factor_keeps_exact_zero_carbon_and_reports(c):
    r = c.put('/api/scenario', json={'emission_factor': 0, 'hours': 4000})
    assert r.status_code == 200, r.text[:300]
    assert r.json()['results']['selected_assessment']['co2_reduction_t'] == 0.0
    assert r.json()['results']['factor_meta']['value'] == 0.0
    report = c.post('/api/reports/export', json={'preset': 'full', 'format':'txt'})
    assert report.status_code == 200, report.text[:200]
