# -*- coding: utf-8 -*-
"""V6 P0 自动化：V6-01 … V6-12。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import store


@pytest.fixture(autouse=True)
def _reset():
    store.reset_all_for_tests()
    yield


def _client() -> TestClient:
    return TestClient(app)


def test_v6_01_default_mill():
    c = _client()
    data = c.get('/api/results').json()
    sa = data['selected_assessment']
    assert sa['saved_kwh'] == 800_000
    assert abs(sa['co2_reduction_t'] - 357.68) < 0.01
    assert data['summary']['saved_kwh'] == 800_000
    assert '甘肃' in (data['factor_meta'].get('label') or '')


def test_v6_02_switch_pump_with_baseline():
    c = _client()
    r = c.put('/api/scenario', json={
        'selected_measure_id': 'rf-pump',
        'pump_baseline_kwh': 200_000,
    })
    assert r.status_code == 200, r.text
    data = r.json()['results']
    sa = data['selected_assessment']
    assert sa['saved_kwh'] == 25860
    assert abs(sa['co2_reduction_t'] - 11.562) < 0.01
    assert data['summary']['saved_kwh'] == 25860
    assert data['summary']['saved_kwh'] != 800_000
    txt = c.post('/api/reports/export', json={'preset': 'one_page', 'format': 'txt'}).text
    assert '25860' in txt
    assert '800000' not in txt and '800,000' not in txt
    assert '357.68' not in txt


def test_v6_03_pump_missing_baseline():
    c = _client()
    r = c.put('/api/scenario', json={'selected_measure_id': 'rf-pump', 'clear_pump_baseline_kwh': True})
    data = r.json()['results']
    sa = data['selected_assessment']
    assert sa['saved_kwh'] is None
    assert data['summary']['saved_kwh'] is None
    assert '泵' in (sa.get('waiting_message') or '') or '用水' in (sa.get('waiting_message') or '') or '补充' in (sa.get('waiting_message') or '')


def test_v6_04_compare_investment_isolation():
    c = _client()
    # 确保球磨有投资、泵无投资
    c.put('/api/scenario', json={'selected_measure_id': 'rf-mill', 'investment_cny': 1_000_000})
    r = c.post('/api/schemes/compare', json={
        'measure_a_id': 'rf-mill',
        'measure_b_id': 'rf-pump',
        'budget_cny': 2_000_000,
    })
    assert r.status_code == 200, r.text
    cmp_ = r.json()
    assert cmp_['A']['investment_cny'] == 1_000_000
    assert cmp_['B']['investment_cny'] is None
    assert cmp_['B']['investment_display'] in ('待补充', '尚未填写')
    assert cmp_['B']['simple_payback_years'] is None
    # 允许仅在已填投资的一侧给出说明；不得给泵确定回收期或把100万算到泵上
    if cmp_.get('recommendation'):
        assert cmp_['recommendation']['measure_id'] != 'rf-pump'


def test_v6_05_vfd_quote_not_full_investment():
    c = _client()
    before = c.get('/api/results').json()['case']['scenario']['investment_cny']
    r = c.post('/api/procurement/apply-quote', json={
        'quote_id': 'q-vfd-wolong',
        'compatibility_result': 'compatible',
        'apply_to_investment': True,
        'confirm_reference_scenario': True,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['results']['case']['scenario']['investment_cny'] == before
    qc = body['quote_choice']
    assert qc['quote_usage_mode'] == 'price_sensitivity'
    assert qc.get('is_full_retrofit_investment') is False
    sa = body['results']['selected_assessment']
    # 不得因 138000 得出约 0.38 年确定回收期
    if sa.get('simple_payback_years') is not None:
        assert abs(sa['simple_payback_years'] - 0.38) > 0.05
    snap = body['results']['assessment_snapshot']
    sens = snap.get('price_sensitivity') or {}
    if sens.get('hypothetical_payback_years') is not None:
        assert sens.get('is_definitive_payback') is False


def test_v6_07_pdf_rejected_no_fake():
    c = _client()
    r = c.post('/api/reports/export', json={'preset': 'one_page', 'format': 'pdf'})
    assert r.status_code == 400
    assert 'PDF' in r.text or 'pdf' in r.text.lower() or 'TXT' in r.text


def test_v6_08_custom_quotes_only():
    c = _client()
    r = c.post('/api/reports/preview', json={'preset': 'custom', 'sections': ['quotes']})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['sections'] == ['quotes']
    text = body['text']
    assert '800000' not in text and '800,000' not in text
    assert '357.68' not in text
    assert '采购报价' in text or '参考报价' in text


def test_v6_09_pump_report_section():
    c = _client()
    c.put('/api/scenario', json={'selected_measure_id': 'rf-pump', 'pump_baseline_kwh': 200_000})
    r = c.post('/api/reports/preview', json={'preset': 'custom', 'sections': ['energy', 'carbon']})
    text = r.json()['text']
    assert '25860' in text
    assert '800000' not in text
    assert '357.68' not in text


def test_v6_10_user_factor_label():
    c = _client()
    r = c.put('/api/scenario', json={'emission_factor': 0.20})
    data = r.json()['results']
    assert abs(data['selected_assessment']['co2_reduction_t'] - 160.0) < 0.01
    assert data['factor_meta']['label'] == '用户提供'
    assert '甘肃' not in data['factor_meta']['label']


def test_v6_11_quote_change_keeps_energy():
    c = _client()
    base = c.get('/api/results').json()['selected_assessment']
    r = c.post('/api/procurement/apply-quote', json={
        'quote_id': 'q-vfd-wolong',
        'confirm_reference_scenario': True,
        'as_price_sensitivity': True,
    })
    assert r.status_code == 200
    sa = r.json()['results']['selected_assessment']
    assert sa['saved_kwh'] == base['saved_kwh']
    assert sa['co2_reduction_t'] == base['co2_reduction_t']


def test_v6_12_new_enterprise_no_silent_tariff():
    from tests.test_v5_quotes_reports import _new_enterprise, _auth
    c = _client()
    _, tok, sid = _new_enterprise(c, '测试矿企')
    h = _auth(tok, sid)
    data = c.get('/api/results', headers=h).json()
    sc = data['case']['scenario']
    sa = data.get('selected_assessment') or {}
    assert not sc.get('use_jinhui_tariff_reference')
    # 无节电或无电价时不得冒出金徽节费
    if sa.get('saved_kwh') is None or sc.get('energy_price') is None:
        assert sa.get('annual_cost_saving_cny') is None


def test_v6_06_sensitivity_note_present():
    c = _client()
    r = c.post('/api/procurement/apply-quote', json={
        'quote_id': 'q-vfd-wolong',
        'confirm_reference_scenario': True,
        'apply_to_investment': True,
    })
    assert r.status_code == 200
    qc = r.json()['quote_choice']
    assert '价格敏感性' in (qc.get('watermark') or '') or qc.get('quote_usage_mode') == 'price_sensitivity'
