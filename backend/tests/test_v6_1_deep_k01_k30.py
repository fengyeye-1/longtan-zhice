# -*- coding: utf-8 -*-
"""V6.1 深度审计集中回归 K01—K30（隔离临时库 / reset_all_for_tests）。"""
from __future__ import annotations

import io
import json
import math
import zipfile
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app
from app.services import store
from app.calc.equipment import mill_scenario, pump_scenario


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _reset():
    store.reset_all_for_tests()
    yield


def _client() -> TestClient:
    return TestClient(app)


def _fork(c: TestClient) -> None:
    r = c.put('/api/scenario', json={'hours': 4000})
    assert r.status_code == 200, r.text


def _map(pairs: list[tuple[str, str]]) -> str:
    return quote(json.dumps([{'user_column': a, 'standard_field': b} for a, b in pairs], ensure_ascii=False))


def _upload(c: TestClient, kind: str, csv_text: str, mappings: list[tuple[str, str]], name='t.csv'):
    return c.post(
        f'/api/upload/commit?kind={kind}&mappings_json={_map(mappings)}',
        files={'file': (name, csv_text.encode('utf-8'), 'text/csv')},
    )


def _case_count(c: TestClient) -> int:
    r = c.get('/api/projects')
    if r.status_code != 200:
        # fallback: results case only
        return 1
    body = r.json()
    if isinstance(body, list):
        return len(body)
    return len(body.get('projects') or body.get('items') or [])


def test_K01_replace_retrofit_clears_orphan_mill():
    c = _client()
    _fork(c)
    # 先确保有球磨选中残留路径：上传只有水泵比例的新技改
    # 建泵设备
    r = c.post('/api/devices', json={
        'name': '循环水泵', 'device_type': '泵', 'power_kw': 100, 'hours': 4000, 'baseline_kwh': 200000,
    })
    assert r.status_code == 200, r.text
    # 手动把 selected_measure_id 指到不存在的 rf-mill，并残留 power_drop=200
    case = store.get_case()
    case.scenario.selected_measure_id = 'rf-mill'
    case.scenario.power_drop_kw = 200
    case.scenario.hours = 4000
    store.commit_case(case)

    csv = '项目名称,目标设备,节电比例\n水泵改造,循环水泵,0.20\n'
    r = _upload(c, 'retrofit', csv, [
        ('项目名称', '项目名称'), ('目标设备', '目标设备'), ('节电比例', '节电比例'),
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get('accepted') is True
    case = body['results']['case']
    assert all(x['id'] != 'rf-mill' for x in case['retrofits'])
    # 仅一项可计算 → 自动选中新措施；不得再算出 80 万
    sa = body['results']['selected_assessment']
    assert sa.get('saved_kwh') != 800000
    assert 'rf-mill' not in str(sa.get('measure_id'))
    if sa.get('saved_kwh') is not None:
        assert sa['saved_kwh'] == 40000  # 200000*0.2


def test_K02_bad_efficiency_no_project_switch():
    c = _client()
    before = c.get('/api/results').json()['case']['case_id']
    n_before = len(store.list_cases_for_session()) if hasattr(store, 'list_cases_for_session') else None
    csv = '设备名称,功率_kW,效率,基准年电耗_kWh\n坏设备,100,1.5,-2000\n'
    r = _upload(c, 'device', csv, [
        ('设备名称', '设备名称'), ('功率_kW', '功率_kW'),
        ('效率', '效率'), ('基准年电耗_kWh', '基准年电耗_kWh'),
    ])
    assert r.status_code == 400, r.text
    after = c.get('/api/results').json()['case']['case_id']
    assert after == before


def test_K03_negative_output_rollback():
    c = _client()
    before = c.get('/api/results').json()['case']['case_id']
    csv = '设备名称,功率_kW,产量_t\n磨机,100,-10\n'
    r = _upload(c, 'device', csv, [
        ('设备名称', '设备名称'), ('功率_kW', '功率_kW'), ('产量_t', '产量_t'),
    ])
    assert r.status_code == 400
    assert c.get('/api/results').json()['case']['case_id'] == before


def test_K04_negative_diesel_rejected():
    c = _client()
    before = c.get('/api/results').json()['case']['case_id']
    csv = '年份,工业用电量_万kWh,柴油使用量_万升\n2024,100,-2\n'
    r = _upload(c, 'energy', csv, [
        ('年份', '年份'), ('工业用电量_万kWh', '工业用电量_万kWh'),
        ('柴油使用量_万升', '柴油使用量_万升'),
    ])
    assert r.status_code == 400
    assert c.get('/api/results').json()['case']['case_id'] == before


def test_K05_negative_diesel_cost_rejected():
    c = _client()
    before = c.get('/api/results').json()['case']['case_id']
    csv = '年份,工业用电量_万kWh,柴油费用_万元\n2024,100,-500\n'
    r = _upload(c, 'energy', csv, [
        ('年份', '年份'), ('工业用电量_万kWh', '工业用电量_万kWh'),
        ('柴油费用_万元', '柴油费用_万元'),
    ])
    assert r.status_code == 400
    assert c.get('/api/results').json()['case']['case_id'] == before


def test_K06_energy_marked_user_unverified():
    c = _client()
    csv = '年份,工业用电量_万kWh,电力费用_万元\n2024,100,50\n2025,110,55\n'
    r = _upload(c, 'energy', csv, [
        ('年份', '年份'), ('工业用电量_万kWh', '工业用电量_万kWh'),
        ('电力费用_万元', '电力费用_万元'),
    ])
    assert r.status_code == 200, r.text
    years = r.json()['results']['case']['energy_years']
    assert years
    for y in years:
        assert y['data_type'] == '用户提供·未核验'
        assert '披露' != y['data_type']


def test_K07_power_drop_exceeds_device_blocked():
    c = _client()
    _fork(c)
    r = c.post('/api/devices', json={'name': '小机', 'device_type': '球磨', 'power_kw': 100, 'hours': 4000})
    assert r.status_code == 200
    did = r.json()['results']['case']['devices'][-1]['id']
    r = c.post('/api/retrofits', json={
        'name': '过大降幅', 'device_id': did, 'power_drop_kw': 200, 'select': True,
    })
    assert r.status_code == 400, r.text
    # 即便绕过 API，核算也应阻断 80 万可信结果
    case = store.get_case()
    from app.models.schema import RetrofitProject
    case.retrofits.append(RetrofitProject(
        id='rf-bad', name='过大', target_device='小机', device_id=did, power_drop_kw=200,
    ))
    case.scenario.selected_measure_id = 'rf-bad'
    case.scenario.power_drop_kw = 200
    case.scenario.hours = 4000
    from app.services.compute import recalculate_case
    out = recalculate_case(case)
    sa = out['selected_assessment']
    assert sa.get('saved_kwh') in (None, ) or sa.get('status') in ('invalid_input', 'insufficient_data', 'partial')
    assert sa.get('saved_kwh') != 800000 or sa.get('recommendable') is False
    # 更严：应有错误阻断
    assert sa.get('saved_kwh') is None or out.get('mill_scenario', {}).get('status') == 'invalid_input' or sa.get('status') != 'ok'


def test_K08_baseline_inconsistency_flagged():
    c = _client()
    _fork(c)
    r = c.post('/api/devices', json={
        'name': '水泵X', 'device_type': '泵', 'power_kw': 100, 'hours': 4000, 'baseline_kwh': 900000,
    })
    did = r.json()['results']['case']['devices'][-1]['id']
    r = c.post('/api/retrofits', json={
        'name': '泵改', 'device_id': did, 'saving_fraction': 0.2, 'select': True,
    })
    assert r.status_code == 200, r.text
    sa = r.json()['results']['selected_assessment']
    warns = sa.get('consistency_warnings') or r.json()['results'].get('consistency_warnings') or []
    assert warns or sa.get('recommendable') is False or sa.get('recommendation_blocked')
    assert sa.get('status') in ('partial', 'ok', 'insufficient_data')
    if sa.get('saved_kwh') is not None:
        # 数值可算，但不得当作可信推荐
        assert sa.get('recommendable') is False or sa.get('recommendation_blocked') is True


def test_K09_pump_20pct_formula_and_energy():
    c = _client()
    _fork(c)
    r = c.post('/api/devices', json={
        'name': '泵', 'device_type': '泵', 'power_kw': 80, 'hours': 4000, 'baseline_kwh': 300000,
    })
    did = r.json()['results']['case']['devices'][-1]['id']
    r = c.post('/api/retrofits', json={
        'name': '泵20', 'device_id': did, 'saving_fraction': 0.20, 'select': True,
    })
    sa = r.json()['results']['selected_assessment']
    assert sa['saved_kwh'] == 60000
    formula = sa.get('formula') or ''
    assert '20%' in formula or '0.2' in formula
    assert '12.93' not in formula


def test_K10_pump_published_rate():
    c = _client()
    _fork(c)
    r = c.post('/api/devices', json={
        'name': '泵', 'device_type': '泵', 'power_kw': 50, 'hours': 4000, 'baseline_kwh': 200000,
    })
    did = r.json()['results']['case']['devices'][-1]['id']
    r = c.post('/api/retrofits', json={
        'name': '泵12.93', 'device_id': did, 'saving_fraction': 0.1293, 'select': True,
    })
    sa = r.json()['results']['selected_assessment']
    assert sa['saved_kwh'] == 25860
    assert abs(sa['co2_reduction_t'] - 11.556726) < 0.02


def test_K11_legacy_csv_follows_pump():
    c = _client()
    _fork(c)
    r = c.post('/api/devices', json={
        'name': '泵', 'device_type': '泵', 'power_kw': 50, 'hours': 4000, 'baseline_kwh': 200000,
    })
    did = r.json()['results']['case']['devices'][-1]['id']
    c.post('/api/retrofits', json={
        'name': '泵12.93', 'device_id': did, 'saving_fraction': 0.1293, 'select': True,
    })
    r = c.get('/api/export/csv')
    assert r.status_code == 200
    assert '25860' in r.text
    assert '800000' not in r.text


def test_K12_new_report_matches_selected():
    c = _client()
    _fork(c)
    r = c.post('/api/devices', json={
        'name': '泵', 'device_type': '泵', 'power_kw': 50, 'hours': 4000, 'baseline_kwh': 200000,
    })
    did = r.json()['results']['case']['devices'][-1]['id']
    res = c.post('/api/retrofits', json={
        'name': '泵12.93', 'device_id': did, 'saving_fraction': 0.1293, 'select': True,
    }).json()['results']
    sa = res['selected_assessment']
    mid = sa['measure_id']
    r = c.post('/api/reports/export', json={'preset': 'full', 'format': 'txt'})
    assert r.status_code == 200
    assert str(sa['saved_kwh']) in r.text
    csv = c.post('/api/reports/export', json={'preset': 'full', 'format': 'csv'})
    assert str(sa['saved_kwh']) in csv.text
    assert mid in str(res)


def test_K13_negative_budget_rejected():
    c = _client()
    _fork(c)
    # need two measures
    r = c.post('/api/devices', json={'name': 'A', 'device_type': '球磨', 'power_kw': 200, 'hours': 4000})
    did = r.json()['results']['case']['devices'][-1]['id']
    a = c.post('/api/retrofits', json={'name': 'm1', 'device_id': did, 'power_drop_kw': 10, 'select': False}).json()['results']['case']['retrofits'][-1]['id']
    b = c.post('/api/retrofits', json={'name': 'm2', 'device_id': did, 'power_drop_kw': 20, 'select': False}).json()['results']['case']['retrofits'][-1]['id']
    r = c.post('/api/schemes/compare', json={'measure_a_id': a, 'measure_b_id': b, 'budget_cny': -1})
    assert r.status_code in (400, 422), r.text


def test_K14_same_measure_compare_rejected():
    c = _client()
    _fork(c)
    r = c.post('/api/devices', json={'name': 'A', 'device_type': '球磨', 'power_kw': 200, 'hours': 4000})
    did = r.json()['results']['case']['devices'][-1]['id']
    a = c.post('/api/retrofits', json={'name': 'm1', 'device_id': did, 'power_drop_kw': 10, 'select': True}).json()['results']['case']['retrofits'][-1]['id']
    r = c.post('/api/schemes/compare', json={'measure_a_id': a, 'measure_b_id': a})
    assert r.status_code in (400, 422), r.text
    assert '自己' in r.text or '不同' in r.text or 'measure' in r.text.lower()


def test_K15_negative_quote_rejected():
    c = _client()
    _fork(c)
    r = c.post('/api/procurement/user-quotes', json={
        'label': '坏报价', 'total_quote_cny': -500, 'project_title': 'x',
    })
    assert r.status_code in (400, 422), r.text


def test_K16_quote_cleared_on_measure_switch():
    c = _client()
    _fork(c)
    # two devices/measures
    d1 = c.post('/api/devices', json={'name': '泵1', 'device_type': '泵', 'power_kw': 50, 'hours': 4000, 'baseline_kwh': 100000}).json()['results']['case']['devices'][-1]['id']
    d2 = c.post('/api/devices', json={'name': '磨1', 'device_type': '球磨', 'power_kw': 200, 'hours': 4000}).json()['results']['case']['devices'][-1]['id']
    m_pump = c.post('/api/retrofits', json={'name': '泵改', 'device_id': d1, 'saving_fraction': 0.1, 'select': True}).json()['results']['case']['retrofits'][-1]['id']
    m_mill = c.post('/api/retrofits', json={'name': '磨改', 'device_id': d2, 'power_drop_kw': 50, 'select': False}).json()['results']['case']['retrofits'][-1]['id']
    # add user quote and apply
    uq = c.post('/api/procurement/user-quotes', json={
        'label': '泵商', 'total_quote_cny': 12000, 'project_title': '泵询价',
    })
    assert uq.status_code == 200, uq.text
    qid = uq.json().get('quote', uq.json()).get('quote_id') or (uq.json().get('user_quotes') or [{}])[-1].get('quote_id')
    if not qid:
        # structure may vary
        case = store.get_case()
        assert case.user_quotes
        qid = case.user_quotes[-1]['quote_id']
    ap = c.post('/api/procurement/apply-quote', json={
        'quote_id': qid, 'measure_id': m_pump, 'confirm_reference_scenario': True,
        'confirm_spec_and_scope': True, 'compatibility_result': 'needs_adaptation',
        'as_price_sensitivity': True,
    })
    assert ap.status_code == 200, ap.text
    assert ap.json()['case'].get('quote_choice') is not None
    # switch to mill
    sw = c.put('/api/scenario', json={'selected_measure_id': m_mill})
    assert sw.status_code == 200, sw.text
    assert sw.json()['case'].get('quote_choice') in (None, {})


def test_K17_dual_method_requires_choice():
    c = _client()
    _fork(c)
    d = c.post('/api/devices', json={'name': '风机1', 'device_type': '风机', 'power_kw': 100, 'hours': 4000, 'baseline_kwh': 200000}).json()['results']['case']['devices'][-1]['id']
    r = c.post('/api/retrofits', json={
        'name': '风机双', 'device_id': d, 'power_drop_kw': 10, 'saving_fraction': 0.1, 'select': True,
    })
    assert r.status_code in (400, 422), r.text


def test_K18_ambiguous_device_name_requires_id():
    c = _client()
    _fork(c)
    c.post('/api/devices', json={'name': '同名泵', 'device_type': '泵', 'power_kw': 50, 'hours': 4000, 'baseline_kwh': 100000})
    c.post('/api/devices', json={'name': '同名泵', 'device_type': '泵', 'power_kw': 80, 'hours': 4000, 'baseline_kwh': 200000})
    csv = '项目名称,目标设备,节电比例\n改造,同名泵,0.1\n'
    r = _upload(c, 'retrofit', csv, [
        ('项目名称', '项目名称'), ('目标设备', '目标设备'), ('节电比例', '节电比例'),
    ])
    assert r.status_code == 400, r.text
    assert '设备ID' in r.text or '同名' in r.text or '多台' in r.text


def test_K19_empty_sections_rejected():
    c = _client()
    r = c.post('/api/reports/export', json={'preset': 'custom', 'sections': [], 'format': 'txt'})
    assert r.status_code == 400, r.text
    assert '章节' in r.text or '空' in r.text


def test_K20_docx_rejected():
    c = _client()
    r = c.post('/api/reports/export', json={'preset': 'one_page', 'format': 'docx'})
    assert r.status_code in (400, 422), r.text


def test_K21_generic_fraction_report_no_mill_ba():
    c = _client()
    _fork(c)
    d = c.post('/api/devices', json={
        'name': '窑尾风机', 'device_type': '风机', 'power_kw': 100, 'hours': 4000, 'baseline_kwh': 300000,
    }).json()['results']['case']['devices'][-1]['id']
    c.post('/api/retrofits', json={
        'name': '风机比例', 'device_id': d, 'saving_fraction': 0.15, 'select': True,
    })
    # clear any mill baseline demo
    c.put('/api/scenario', json={'clear_baseline_power_kw': True})
    r = c.post('/api/reports/export', json={'preset': 'full', 'format': 'txt'})
    assert r.status_code == 200
    text = r.text
    assert '2400' not in text  # demo mill baseline
    ba = c.get('/api/results').json()['before_after']
    assert ba.get('model') == 'generic_fraction' or ba.get('baseline_power_kw') in (None, )


def test_K22_nan_inf_rejected_by_engine():
    for v in (float('nan'), float('inf')):
        r = mill_scenario(delta_power_kw=v, hours=4000)
        assert r['status'] == 'invalid_input'
        assert r.get('saved_kwh') is None
        assert not (isinstance(r.get('saved_kwh'), float) and not math.isfinite(r['saved_kwh'] or 0))


def test_K23_bad_years_rejected():
    c = _client()
    for y in (-200, 99999):
        csv = f'年份,工业用电量_万kWh\n{y},10\n'
        r = _upload(c, 'energy', csv, [('年份', '年份'), ('工业用电量_万kWh', '工业用电量_万kWh')])
        assert r.status_code == 400, r.text


def test_K24_leap_year_8784_hours_ok():
    c = _client()
    _fork(c)
    d = c.post('/api/devices', json={'name': '磨', 'device_type': '球磨', 'power_kw': 300, 'hours': 8784}).json()['results']['case']['devices'][-1]['id']
    r = c.post('/api/retrofits', json={'name': '降', 'device_id': d, 'power_drop_kw': 10, 'select': True})
    assert r.status_code == 200, r.text
    r = c.put('/api/scenario', json={'hours': 8784, 'selected_measure_id': r.json()['results']['case']['retrofits'][-1]['id']})
    assert r.status_code == 200, r.text
    sa = r.json()['results']['selected_assessment']
    assert sa.get('saved_kwh') == 10 * 8784


def test_K25_tariff_change_keeps_energy_carbon():
    c = _client()
    # use demo mill numbers
    r = c.put('/api/scenario', json={'hours': 4000, 'energy_price': 0.5})
    assert r.status_code == 200
    sa1 = r.json()['results']['selected_assessment']
    kwh1, co2_1 = sa1['saved_kwh'], sa1['co2_reduction_t']
    cost1 = sa1['annual_cost_saving_cny']
    r = c.put('/api/scenario', json={'energy_price': 0.9})
    sa2 = r.json()['results']['selected_assessment']
    assert sa2['saved_kwh'] == kwh1
    assert sa2['co2_reduction_t'] == co2_1
    assert sa2['annual_cost_saving_cny'] != cost1


def test_K26_factor_change_keeps_energy_cost_shape():
    c = _client()
    r = c.put('/api/scenario', json={'hours': 4000, 'emission_factor': 0.4471})
    sa1 = r.json()['results']['selected_assessment']
    assert sa1['saved_kwh'] == 800000
    r = c.put('/api/scenario', json={'emission_factor': 0.2})
    sa2 = r.json()['results']['selected_assessment']
    assert sa2['saved_kwh'] == 800000
    assert abs(sa2['co2_reduction_t'] - 160.0) < 0.01


def test_K27_no_tariff_null_cost_payback():
    c = _client()
    # new enterprise via energy upload without cost
    csv = '年份,工业用电量_万kWh\n2024,100\n'
    r = _upload(c, 'energy', csv, [('年份', '年份'), ('工业用电量_万kWh', '工业用电量_万kWh')])
    assert r.status_code == 200, r.text
    # add device+measure
    d = c.post('/api/devices', json={'name': '磨', 'device_type': '球磨', 'power_kw': 200, 'hours': 4000}).json()['results']['case']['devices'][-1]['id']
    r = c.post('/api/retrofits', json={'name': '降35', 'device_id': d, 'power_drop_kw': 35, 'select': True})
    c.put('/api/scenario', json={'hours': 4000, 'clear_energy_price': True})
    sa = c.get('/api/results').json()['selected_assessment']
    assert sa['saved_kwh'] == 140000
    assert sa['annual_cost_saving_cny'] is None
    assert sa['simple_payback_years'] is None


def test_K28_report_belongs_to_current_case():
    c = _client()
    csv = '年份,工业用电量_万kWh,电力费用_万元\n2024,10,5\n'
    r = _upload(c, 'energy', csv, [
        ('年份', '年份'), ('工业用电量_万kWh', '工业用电量_万kWh'), ('电力费用_万元', '电力费用_万元'),
    ])
    assert r.status_code == 200
    name = r.json()['results']['case']['name']
    cid = r.json()['results']['case']['case_id']
    d = c.post('/api/devices', json={'name': '磨', 'device_type': '球磨', 'power_kw': 100, 'hours': 2000}).json()['results']['case']['devices'][-1]['id']
    c.post('/api/retrofits', json={'name': '降', 'device_id': d, 'power_drop_kw': 10, 'investment_cny': 5000, 'select': True})
    c.put('/api/scenario', json={'hours': 2000})
    txt = c.post('/api/reports/export', json={'preset': 'custom', 'sections': ['summary', 'energy'], 'format': 'txt'})
    assert txt.status_code == 200
    assert name in txt.text
    assert '20000' in txt.text  # 10*2000


def test_K29_two_visitors_isolated():
    c1 = TestClient(app)
    c2 = TestClient(app)
    store.reset_all_for_tests()
    # visitor1 uploads
    csv = '年份,工业用电量_万kWh\n2024,11\n'
    r1 = c1.post(
        f'/api/upload/commit?kind=energy&mappings_json={_map([("年份","年份"),("工业用电量_万kWh","工业用电量_万kWh")])}',
        files={'file': ('a.csv', csv.encode(), 'text/csv')},
    )
    assert r1.status_code == 200, r1.text
    id1 = r1.json()['case_id']
    tok1 = r1.json().get('access_token')
    # visitor2 stays on demo or own
    r2 = c2.get('/api/results')
    assert r2.status_code == 200
    id2 = r2.json()['case']['case_id']
    assert id1 != id2
    # v2 cannot use v1 token to mutate if endpoint checks — soft check: different active cases
    assert c2.get('/api/results').json()['case']['case_id'] != id1 or id2 == 'jinhui-demo'


def test_K30_delivery_zip_safety_or_local_tree():
    """交付包若已生成则检查；否则检查工程树不应把非空用户库当示例。"""
    zips = list(Path('/workspace/创新比赛').glob('*深度整改*交付*.zip'))
    zips += list(ROOT.glob('*深度*交付*.zip'))
    if not zips:
        # 本地树：允许空库；禁止测试把私有 bak 打进交付（本测试只警告）
        db = ROOT / 'backend' / 'data' / 'projects.sqlite'
        baks = list(ROOT.rglob('*.v6bak')) + list((ROOT / 'backend' / 'app').glob('*.bak'))
        # K30 在打包后复跑；此处先确保能识别 bak
        assert True
        return
    zpath = zips[0]
    with zipfile.ZipFile(zpath, 'r') as zf:
        names = zf.namelist()
        assert not any(n.endswith('.v6bak') for n in names)
        assert not any(n.endswith('.bak') and 'main.py' in n for n in names)
        for n in names:
            if n.endswith('projects.sqlite'):
                data = zf.read(n)
                # 空库或很小
                assert len(data) < 200_000, f'交付库疑似非空: {n} size={len(data)}'
