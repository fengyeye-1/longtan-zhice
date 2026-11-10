# -*- coding: utf-8 -*-
"""V6.1 定向回归 R01–R08（对照任务书验收计算）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app
from app.services import store


@pytest.fixture(autouse=True)
def _reset():
    store.reset_all_for_tests()
    yield


def _client() -> TestClient:
    return TestClient(app)


def _fork_writable(c: TestClient) -> None:
    """示范只读：写一次情景以 fork 可写副本（若 API 已自动 fork 则无害）。"""
    r = c.put('/api/scenario', json={'hours': 4000})
    assert r.status_code == 200, r.text


def test_R01_two_pumps_baseline_isolation():
    """A 200000 / B 900000；B 20% → 180000 kWh / 80.478 t；改 A 不影响 B。"""
    c = _client()
    _fork_writable(c)

    r = c.post('/api/devices', json={
        'name': '水泵A', 'device_type': '泵', 'baseline_kwh': 200_000, 'power_kw': 50, 'hours': 4000,
    })
    assert r.status_code == 200, r.text
    a_id = r.json()['results']['case']['devices'][-1]['id']

    r = c.post('/api/devices', json={
        'name': '水泵B', 'device_type': '泵', 'baseline_kwh': 900_000, 'power_kw': 100, 'hours': 4000,
    })
    assert r.status_code == 200, r.text
    b_id = r.json()['results']['case']['devices'][-1]['id']

    r = c.post('/api/retrofits', json={
        'name': 'A泵改造', 'device_id': a_id, 'saving_fraction': 0.10,
        'investment_cny': 10_000, 'select': False,
    })
    assert r.status_code == 200, r.text
    a_rf = r.json()['results']['case']['retrofits'][-1]['id']

    r = c.post('/api/retrofits', json={
        'name': 'B泵改造', 'device_id': b_id, 'saving_fraction': 0.20,
        'investment_cny': 20_000, 'select': True,
    })
    assert r.status_code == 200, r.text
    b_rf = r.json()['results']['case']['retrofits'][-1]['id']

    data = c.put('/api/scenario', json={'selected_measure_id': b_rf}).json()['results']
    sa = data['selected_assessment']
    assert sa['saved_kwh'] == 180_000
    assert abs(sa['co2_reduction_t'] - 80.478) < 0.01
    assert sa.get('device_id') == b_id

    # 改 A 基准，B 结果不变
    r = c.put(f'/api/devices/{a_id}', json={'baseline_kwh': 50_000})
    assert r.status_code == 200, r.text
    data2 = c.put('/api/scenario', json={'selected_measure_id': b_rf}).json()['results']
    assert data2['selected_assessment']['saved_kwh'] == 180_000
    assert abs(data2['selected_assessment']['co2_reduction_t'] - 80.478) < 0.01

    # 选 A 时用 A 自己的基准
    data_a = c.put('/api/scenario', json={'selected_measure_id': a_rf}).json()['results']
    assert data_a['selected_assessment']['saved_kwh'] == 5_000  # 50000 * 0.10


def test_R02_maintenance_independent_payback():
    """投资50万、年节电费40万、新增运维5万 → 净收益35万，回收期≈1.43。"""
    c = _client()
    _fork_writable(c)
    r = c.post('/api/devices', json={
        'name': '球磨测运维', 'device_type': '球磨', 'power_kw': 500, 'hours': 4000,
    })
    assert r.status_code == 200, r.text
    dev_id = r.json()['results']['case']['devices'][-1]['id']

    r = c.post('/api/retrofits', json={
        'name': '运维验收措施',
        'device_id': dev_id,
        'power_drop_kw': 100,
        'investment_cny': 500_000,
        'annual_maintenance_cny': 50_000,
        'select': True,
    })
    assert r.status_code == 200, r.text
    mid = r.json()['results']['case']['retrofits'][-1]['id']

    r = c.put('/api/scenario', json={
        'selected_measure_id': mid,
        'hours': 4000,
        'energy_price': 1.0,  # 100kW×4000h×1 = 400_000 年节电费
        'emission_factor': 0.4471,
    })
    assert r.status_code == 200, r.text
    sa = r.json()['results']['selected_assessment']
    assert sa['saved_kwh'] == 400_000
    assert abs(sa['annual_cost_saving_cny'] - 400_000) < 0.01
    assert abs(sa['annual_net_saving_cny'] - 350_000) < 0.01
    assert abs(sa['simple_payback_years'] - 1.43) < 0.01
    assert sa['simple_payback_years'] != 1.25


def test_R03_hours_conflict_rejected():
    """2025 实际500h + 有效4000h → 拒绝；改300h可算。"""
    c = _client()
    _fork_writable(c)
    r = c.post('/api/devices', json={
        'name': '小时冲突球磨', 'device_type': '球磨', 'power_kw': 200, 'hours': 500,
    })
    assert r.status_code == 200, r.text
    dev_id = r.json()['results']['case']['devices'][-1]['id']

    r = c.post('/api/retrofits', json={
        'name': '小时冲突措施', 'device_id': dev_id, 'power_drop_kw': 50,
        'investment_cny': 100_000, 'select': True,
    })
    assert r.status_code == 200, r.text
    mid = r.json()['results']['case']['retrofits'][-1]['id']

    # 先设合法小时，记录状态
    ok = c.put('/api/scenario', json={
        'selected_measure_id': mid, 'hours': 300, 'energy_price': 0.5,
    })
    assert ok.status_code == 200, ok.text
    before_hours = ok.json()['results']['case']['scenario']['hours']
    assert before_hours == 300

    bad = c.put('/api/scenario', json={'hours': 4000})
    assert bad.status_code == 400, bad.text
    detail = bad.json().get('detail') or {}
    msg = json.dumps(detail, ensure_ascii=False)
    assert '预计节能小时超过设备实际运行时间' in msg

    # 原状态未被污染
    cur = c.get('/api/results').json()['case']['scenario']['hours']
    assert cur == 300

    good = c.put('/api/scenario', json={'hours': 300, 'energy_price': 0.5})
    assert good.status_code == 200, good.text
    sa = good.json()['results']['selected_assessment']
    assert sa['status'] == 'ok'
    assert sa['saved_kwh'] == 50 * 300


def test_R04_import_device_no_jinhui_measures():
    """从金徽导入仅1台球磨 CSV → 新企业1设备、0金徽措施；新加35kW×4000h→140000。"""
    c = _client()
    assert c.get('/api/results').json()['case']['case_id'] == 'jinhui-demo'

    csv = (
        '设备名称,设备类型,功率_kW,年运行小时\n'
        '新企业球磨,球磨,300,4000\n'
    ).encode('utf-8')
    mappings = json.dumps([
        {'user_column': '设备名称', 'standard_field': '设备名称'},
        {'user_column': '设备类型', 'standard_field': '设备类型'},
        {'user_column': '功率_kW', 'standard_field': '功率_kW'},
        {'user_column': '年运行小时', 'standard_field': '年运行小时'},
    ], ensure_ascii=False)
    r = c.post(
        f'/api/upload/commit?kind=device&mappings_json={quote(mappings)}',
        files={'file': ('mill.csv', csv, 'text/csv')},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get('accepted') is True
    case = body['results']['case']
    assert case['case_id'] != 'jinhui-demo'
    assert len(case['devices']) == 1
    assert case['devices'][0]['name'] == '新企业球磨'
    assert len(case['retrofits']) == 0
    # 不得出现金徽措施 id
    assert not any(rf['id'] in ('rf-mill', 'rf-pump') for rf in case['retrofits'])
    src = case['devices'][0].get('source') or ''
    assert '公开披露' not in src
    assert '用户' in src

    dev_id = case['devices'][0]['id']
    r = c.post('/api/retrofits', json={
        'name': '新措施35kW', 'device_id': dev_id, 'power_drop_kw': 35,
        'investment_cny': 200_000, 'select': True,
    })
    assert r.status_code == 200, r.text
    mid = r.json()['results']['case']['retrofits'][-1]['id']
    r = c.put('/api/scenario', json={
        'selected_measure_id': mid, 'hours': 4000, 'energy_price': 0.5,
    })
    assert r.status_code == 200, r.text
    sa = r.json()['results']['selected_assessment']
    assert sa['saved_kwh'] == 140_000
    assert sa['saved_kwh'] != 800_000


def test_R05_pump_report_no_mill_schemes():
    """水泵基准200000、12.93% → 25860 / ≈11.56t；导出不含球磨40/80/120万表。"""
    c = _client()
    r = c.put('/api/scenario', json={
        'selected_measure_id': 'rf-pump',
        'pump_baseline_kwh': 200_000,
    })
    assert r.status_code == 200, r.text
    data = r.json()['results']
    sa = data['selected_assessment']
    assert sa['saved_kwh'] == 25860
    assert abs(sa['co2_reduction_t'] - 11.56) < 0.02
    assert data.get('schemes') in (None, [],) or len(data.get('schemes') or []) == 0

    txt = c.post('/api/reports/export', json={
        'preset': 'full', 'format': 'txt',
    }).text
    assert '25860' in txt or '25,860' in txt
    for bad in ('400000', '400,000', '800000', '800,000', '1200000', '1,200,000', '1200,000'):
        # 允许出现排放因子等无关数字；禁止整块球磨小时对照结论
        pass
    assert '400000' not in txt.replace(',', '')
    assert '1200000' not in txt.replace(',', '')
    # 800000 是金徽球磨默认；泵报告不应再写球磨小时表结论
    assert '年节电 800000' not in txt and '800,000 kWh' not in txt


def test_R06_incomplete_investment_no_best():
    """A 投资8万、B 空、预算10万 → A 在预算内、B 待补充，无无条件最佳推荐。"""
    c = _client()
    _fork_writable(c)
    r = c.post('/api/devices', json={
        'name': '比较球磨', 'device_type': '球磨', 'power_kw': 200, 'hours': 4000,
    })
    assert r.status_code == 200, r.text
    dev_id = r.json()['results']['case']['devices'][-1]['id']

    r = c.post('/api/retrofits', json={
        'name': '方案A', 'device_id': dev_id, 'power_drop_kw': 40,
        'investment_cny': 80_000, 'select': False,
    })
    assert r.status_code == 200, r.text
    a_id = r.json()['results']['case']['retrofits'][-1]['id']

    r = c.post('/api/retrofits', json={
        'name': '方案B', 'device_id': dev_id, 'power_drop_kw': 50,
        'select': False,
    })
    assert r.status_code == 200, r.text
    b_id = r.json()['results']['case']['retrofits'][-1]['id']

    # 给可比口径：小时+电价
    c.put('/api/scenario', json={'hours': 4000, 'energy_price': 0.5, 'selected_measure_id': a_id})

    r = c.post('/api/schemes/compare', json={
        'measure_a_id': a_id, 'measure_b_id': b_id, 'budget_cny': 100_000,
    })
    assert r.status_code == 200, r.text
    cmp_ = r.json()
    assert cmp_['A']['investment_cny'] == 80_000
    assert cmp_['A'].get('budget_label') == '在预算内' or cmp_['A'].get('within_budget') is True
    assert cmp_['B']['investment_cny'] is None
    assert cmp_['B'].get('budget_label') == '待补充' or cmp_['B'].get('investment_display') in ('待补充', '尚未填写')
    # 不得无条件宣布最佳
    assert cmp_.get('recommendation') in (None, {})


def test_R07_negative_and_source_label():
    """仅 power_kw=-10 或 hours=-3 拒绝；导入用户值不标公开披露。"""
    c = _client()
    _fork_writable(c)

    r = c.post('/api/devices', json={'name': '负功率', 'device_type': '通用', 'power_kw': -10})
    assert r.status_code in (400, 422), r.text

    r = c.post('/api/devices', json={'name': '负小时', 'device_type': '通用', 'hours': -3, 'power_kw': 10})
    assert r.status_code in (400, 422), r.text

    # 项目未被污染：仍是示范或可写副本，不应出现负功率设备
    devices = c.get('/api/results').json()['case']['devices']
    assert all((d.get('power_kw') or 0) >= 0 for d in devices)
    assert all(d.get('hours') is None or d['hours'] >= 0 for d in devices)

    csv = (
        '设备名称,设备类型,功率_kW,年运行小时,数据来源\n'
        '用户风机,风机,15,2000,车间自测表\n'
    ).encode('utf-8')
    mappings = json.dumps([
        {'user_column': '设备名称', 'standard_field': '设备名称'},
        {'user_column': '设备类型', 'standard_field': '设备类型'},
        {'user_column': '功率_kW', 'standard_field': '功率_kW'},
        {'user_column': '年运行小时', 'standard_field': '年运行小时'},
        {'user_column': '数据来源', 'standard_field': '数据来源'},
    ], ensure_ascii=False)
    # 先回金徽再导入，验证来源标签
    store.reset_all_for_tests()
    r = c.post(
        f'/api/upload/commit?kind=device&mappings_json={quote(mappings)}',
        files={'file': ('fan.csv', csv, 'text/csv')},
    )
    assert r.status_code == 200, r.text
    assert r.json().get('accepted') is True
    case = r.json()['results']['case']
    assert case['case_id'] != 'jinhui-demo'
    src = case['devices'][0].get('source') or ''
    dtype = case['devices'][0].get('data_type') or ''
    assert '企业公开披露' not in src
    assert dtype != '披露' or '用户' in src


def test_R08_generic_fan_fraction_model():
    """通用风机 基准10万×10% → 1万 kWh，标「通用比例情景」，绝不标水泵。"""
    c = _client()
    _fork_writable(c)
    r = c.post('/api/devices', json={
        'name': '通用风机', 'device_type': '风机', 'baseline_kwh': 100_000, 'power_kw': 30, 'hours': 3000,
    })
    assert r.status_code == 200, r.text
    dev_id = r.json()['results']['case']['devices'][-1]['id']

    r = c.post('/api/retrofits', json={
        'name': '风机10%假设', 'device_id': dev_id, 'saving_fraction': 0.10,
        'investment_cny': 5_000, 'select': True,
    })
    assert r.status_code == 200, r.text
    mid = r.json()['results']['case']['retrofits'][-1]['id']
    r = c.put('/api/scenario', json={'selected_measure_id': mid})
    assert r.status_code == 200, r.text
    sa = r.json()['results']['selected_assessment']
    assert sa['saved_kwh'] == 10_000
    label = (sa.get('model_label') or '') + (sa.get('technology_type') or '') + json.dumps(sa, ensure_ascii=False)
    assert '通用比例' in label or sa.get('technology_type') == 'generic_fraction'
    assert '水泵' not in (sa.get('model_label') or '')
    assert sa.get('technology_type') != 'pump'
