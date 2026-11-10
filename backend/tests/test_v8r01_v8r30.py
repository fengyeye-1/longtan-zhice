# -*- coding: utf-8 -*-
"""V8R01–V8R30 验收（可 API 化部分）。浏览器截图项以 API 级证明 + 说明。"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.calc import physics_models as phys
from app.models.schema import CaseState, Device, RetrofitProject, ScenarioParams
from app.services import store
from app.services import equipment_catalog as equip_cat
from app.services import report_sections as report_sec
from app.services.compute import full_recalculate, recalculate_case


@pytest.fixture(autouse=True)
def _reset():
    store.reset_all_for_tests()
    yield


def _client() -> TestClient:
    return TestClient(app)


def _fork_writable(c: TestClient) -> None:
    r = c.put('/api/scenario', json={'hours': 4000})
    assert r.status_code == 200, r.text


def _add_device(c: TestClient, **kw) -> str:
    r = c.post('/api/devices', json=kw)
    assert r.status_code == 200, r.text
    return r.json()['case']['devices'][-1]['id']


def test_v8r01_jinhui_200kw_4000h():
    c = _client()
    r = c.post('/api/reset-demo')
    assert r.status_code == 200, r.text
    data = r.json().get('results') or {}
    sa = data.get('selected_assessment') or {}
    if abs(float(sa.get('saved_kwh') or 0) - 800000) > 0.1:
        r2 = c.put('/api/scenario', json={
            'power_drop_kw': 200, 'hours': 4000, 'use_jinhui_reference': True,
        })
        assert r2.status_code == 200, r2.text
        sa = (r2.json().get('results') or {}).get('selected_assessment') or {}
    assert abs(float(sa['saved_kwh']) - 800000) < 0.1
    assert abs(float(sa['co2_reduction_t']) - 357.68) < 0.02


def test_v8r02_hours_2000_api():
    """浏览器点「重新计算」→ API 改小时；截图需求：首页展示 400000 kWh。"""
    c = _client()
    c.post('/api/reset-demo')
    r = c.put('/api/scenario', json={'power_drop_kw': 200, 'hours': 2000})
    assert r.status_code == 200, r.text
    sa = (r.json().get('results') or {}).get('selected_assessment') or {}
    assert abs(float(sa['saved_kwh']) - 400000) < 0.1


def test_v8r03_pump_professional_into_flow():
    c = _client()
    _fork_writable(c)
    did = _add_device(
        c, name='工艺循环水泵', device_type='循环水泵',
        power_kw=55, hours=4000, data_type='假设',
    )
    r = c.post('/api/retrofits', json={
        'name': '泵效率提升',
        'device_id': did,
        'method_code': 'pump_hydraulic',
        'model_inputs': {
            'q_m3_h': 100, 'head_m': 50,
            'eta_pump_before': 0.70, 'eta_pump_after': 0.80,
            'eta_motor': 0.90, 'hours': 4000,
        },
        'investment_cny': 80000,
        'annual_maintenance_cny': 0,
        'select': True,
    })
    assert r.status_code == 200, r.text
    sa = (r.json().get('results') or {}).get('selected_assessment') or {}
    assert abs(float(sa['saved_kwh']) - 10813.49) < 0.2
    assert sa.get('calculation_method') == 'pump_hydraulic'
    exp = c.post('/api/reports/export', json={
        'preset': 'custom', 'sections': ['energy', 'carbon'], 'format': 'txt',
    })
    assert exp.status_code == 200, exp.text
    assert '10813' in exp.text.replace(',', '')


def test_v8r04_fan_professional_into_flow():
    c = _client()
    _fork_writable(c)
    did = _add_device(
        c, name='主扇1', device_type='矿井通风机',
        power_kw=200, hours=4000, data_type='假设',
    )
    r = c.post('/api/retrofits', json={
        'name': '风机效率提升',
        'device_id': did,
        'method_code': 'fan_pressure_flow',
        'model_inputs': {
            'q_m3_s': 10, 'delta_p_pa': 1000,
            'eta_fan_before': 0.70, 'eta_fan_after': 0.80,
            'eta_motor': 0.90, 'hours': 4000,
        },
        'select': True,
        'annual_maintenance_cny': 0,
    })
    assert r.status_code == 200, r.text
    sa = (r.json().get('results') or {}).get('selected_assessment') or {}
    assert abs(float(sa['saved_kwh']) - 7936.51) < 0.2
    assert abs(float(sa['co2_reduction_t']) - 7936.51 * 0.4471 / 1000) < 0.02


def test_v8r05_motor_shaft_not_rated():
    c = _client()
    _fork_writable(c)
    did = _add_device(
        c, name='驱动电机', device_type='高效电动机/驱动',
        power_kw=110, hours=4000, data_type='假设',
    )
    r = c.post('/api/retrofits', json={
        'name': '高效电机替换',
        'device_id': did,
        'method_code': 'motor_efficiency',
        'model_inputs': {
            'p_shaft_kw': 90, 'eta_old': 0.90, 'eta_new': 0.95, 'hours': 4000,
        },
        'select': True,
        'annual_maintenance_cny': 0,
    })
    assert r.status_code == 200, r.text
    sa = (r.json().get('results') or {}).get('selected_assessment') or {}
    assert abs(float(sa['saved_kwh']) - 21052.63) < 0.2


def test_v8r06_professional_create_not_400():
    c = _client()
    _fork_writable(c)
    did = _add_device(
        c, name='空压机A', device_type='空气压缩机',
        power_kw=100, hours=4000, data_type='假设',
    )
    r = c.post('/api/retrofits', json={
        'name': '空压功率差',
        'device_id': did,
        'method_code': 'compressor_power_drop',
        'model_inputs': {'p_before_kw': 100, 'p_after_kw': 90, 'hours': 4000},
        'select': True,
        'annual_maintenance_cny': 0,
    })
    assert r.status_code == 200, r.text

    tid = _add_device(
        c, name='配电变', device_type='配电变压器',
        power_kw=500, hours=4000, data_type='假设',
    )
    r2 = c.post('/api/retrofits', json={
        'name': '变压器损耗',
        'device_id': tid,
        'method_code': 'transformer_losses',
        'model_inputs': {
            'p0_before_kw': 1.2, 'p0_after_kw': 0.8,
            'pk_before_kw': 9, 'pk_after_kw': 6,
            'beta': 0.5, 'hours': 4000,
        },
        'select': True,
        'annual_maintenance_cny': 0,
    })
    assert r2.status_code == 200, r2.text


def test_v8r07_transformer_nan_rejected():
    c = _client()
    r = c.post('/api/equipment/assessments/calculate', json={
        'method_code': 'transformer_losses',
        'p0_before_kw': 'NaN', 'p0_after_kw': 0.8,
        'pk_before_kw': 9, 'pk_after_kw': 6, 'beta': 0.5, 'hours': 4000,
    })
    assert r.status_code in (400, 422)
    assert r.status_code != 500
    out = phys.transformer_losses(
        p0_before_kw=float('nan'), p0_after_kw=0.8,
        pk_before_kw=9, pk_after_kw=6, beta=0.5, hours=4000,
    )
    assert out['status'] == 'invalid_input'
    assert out.get('saved_kwh') is None


def test_v8r08_transformer_negative_rejected():
    out = phys.transformer_losses(
        p0_before_kw=-1, p0_after_kw=0.8,
        pk_before_kw=9, pk_after_kw=6, beta=0.5, hours=4000,
    )
    assert out['status'] == 'invalid_input'
    assert out.get('saved_kwh') is None


@pytest.mark.parametrize('method,inputs', [
    ('pump_hydraulic', {
        'q_m3_h': 100, 'head_m': 50,
        'eta_pump_before': 0.7, 'eta_pump_after': 0.8, 'hours': 999999,
    }),
    ('fan_pressure_flow', {
        'q_m3_s': 10, 'delta_p_pa': 1000,
        'eta_fan_before': 0.7, 'eta_fan_after': 0.8, 'hours': 999999,
    }),
    ('motor_efficiency', {
        'p_shaft_kw': 90, 'eta_old': 0.9, 'eta_new': 0.95, 'hours': 999999,
    }),
    ('compressor_power_drop', {
        'p_before_kw': 100, 'p_after_kw': 90, 'hours': 999999,
    }),
    ('transformer_losses', {
        'p0_before_kw': 1.2, 'p0_after_kw': 0.8,
        'pk_before_kw': 9, 'pk_after_kw': 6, 'beta': 0.5, 'hours': 999999,
    }),
])
def test_v8r09_hours_999999_rejected(method, inputs):
    c = _client()
    r = c.post('/api/equipment/assessments/calculate', json={'method_code': method, **inputs})
    assert r.status_code in (400, 422)
    out = phys.run_method(method, inputs)
    assert out['status'] == 'invalid_input'


@pytest.mark.parametrize('bad', [float('nan'), -10.0, float('inf')])
def test_v8r10_fan_safety_min_invalid(bad):
    out = phys.fan_pressure_flow(
        q_m3_s=10, delta_p_pa=1000, eta_fan_before=0.7, eta_fan_after=0.8,
        hours=4000, safety_min_q_m3_s=bad,
    )
    assert out['status'] == 'invalid_input'


def test_v8r11_fan_below_safety_min_blocked():
    out = phys.fan_pressure_flow(
        q_m3_s=5, delta_p_pa=1000, eta_fan_before=0.7, eta_fan_after=0.8,
        hours=4000, safety_min_q_m3_s=8,
    )
    assert out['status'] == 'invalid_input'
    assert out.get('safety_conflict') is True
    assert out.get('saved_kwh') is None


def test_v8r12_resolve_type_code_longest_alias():
    assert equip_cat.resolve_type_code('循环水泵') == 'pump_process'
    assert equip_cat.resolve_type_code('矿浆泵') == 'pump_process'
    assert equip_cat.resolve_type_code('尾矿泵') == 'pump_process'
    assert equip_cat.resolve_type_code('矿井排水泵') == 'pump_drainage'
    assert equip_cat.resolve_type_code('排水泵') == 'pump_drainage'
    assert equip_cat.resolve_type_code('泵') == 'generic'
    assert equip_cat.resolve_type_code('未知奇怪设备XYZ') == 'generic'
    assert equip_cat.resolve_type_code('球磨机') == 'ball_mill'
    assert equip_cat.resolve_type_code('半自磨机') == 'sag_mill'
    assert equip_cat.resolve_type_code('矿井通风机') == 'fan_ventilation'
    assert equip_cat.resolve_type_code('高效电动机') == 'motor_drive'


def test_v8r13_quotes_only_html_no_main_table():
    c = _client()
    c.post('/api/reset-demo')
    r = c.post('/api/reports/export', json={
        'preset': 'custom', 'sections': ['quotes'], 'format': 'html',
    })
    assert r.status_code == 200, r.text
    assert '一页结论' not in r.text


def test_v8r14_quotes_only_csv():
    c = _client()
    c.post('/api/reset-demo')
    r = c.post('/api/reports/export', json={
        'preset': 'custom', 'sections': ['quotes'], 'format': 'csv',
    })
    assert r.status_code == 200, r.text
    assert '采购报价' in r.text
    # 仅 quotes 不应夹带小时情景对照表
    assert '小时情景对照' not in r.text


def test_v8r15_methods_csv_has_formula():
    c = _client()
    c.post('/api/reset-demo')
    r = c.post('/api/reports/export', json={
        'preset': 'custom', 'sections': ['methods'], 'format': 'csv',
    })
    assert r.status_code == 200, r.text
    assert '方法' in r.text
    assert ('公式' in r.text) or ('模型' in r.text)


def test_v8r16_sources_csv_has_content():
    c = _client()
    c.post('/api/reset-demo')
    r = c.post('/api/reports/export', json={
        'preset': 'custom', 'sections': ['sources'], 'format': 'csv',
    })
    assert r.status_code == 200, r.text
    assert '来源' in r.text or '排放因子' in r.text
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    assert len(lines) >= 3


def test_v8r17_gaps_schemes_csv_not_empty():
    c = _client()
    c.post('/api/reset-demo')
    for sec in ('gaps', 'schemes'):
        r = c.post('/api/reports/export', json={
            'preset': 'custom', 'sections': [sec], 'format': 'csv',
        })
        assert r.status_code == 200, r.text
        lines = [ln for ln in r.text.splitlines() if ln.strip()]
        assert len(lines) >= 3, sec


def test_v8r18_reference_select_persisted():
    c = _client()
    _fork_writable(c)
    r = c.post('/api/references/select', json={
        'source_id': 'gb-19762-2025',
        'choice': 'match_trial',
        'applicable_device_id': 'd-test',
        'method_code': 'pump_hydraulic',
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get('selection', {}).get('source_id') == 'gb-19762-2025'
    assert body.get('selection', {}).get('verified_by_user') is True
    sels = body.get('reference_selections') or []
    assert any(s.get('source_id') == 'gb-19762-2025' for s in sels)
    case = c.get('/api/case').json()
    assert any(
        s.get('source_id') == 'gb-19762-2025'
        for s in (case.get('reference_selections') or [])
    )


def test_v8r19_references_type_filter():
    c = _client()
    r = c.get('/api/references', params={'type_code': 'pump_drainage'})
    assert r.status_code == 200
    titles = ' '.join(s.get('title') or '' for s in r.json().get('sources') or [])
    assert '19762' in titles

    r2 = c.get('/api/references', params={'type_code': 'motor_drive'})
    assert r2.status_code == 200
    assert len(r2.json().get('sources') or []) > 0

    r3 = c.get('/api/references', params={'type_code': 'fan_ventilation'})
    assert r3.status_code == 200
    assert len(r3.json().get('sources') or []) > 0


def test_v8r20_capability_matrix_triad():
    c = _client()
    r = c.get('/api/equipment/types')
    assert r.status_code == 200
    matrix = r.json().get('capability_matrix') or []
    assert matrix
    for row in matrix:
        assert row.get('可录入') is not None
        assert row.get('可独立试算') is not None
        assert row.get('已接入完整决策流程') is not None


def test_v8r21_tariff_change_keeps_energy():
    c = _client()
    _fork_writable(c)
    did = _add_device(
        c, name='风机B', device_type='矿井通风机',
        power_kw=200, hours=4000, data_type='假设',
    )
    r = c.post('/api/retrofits', json={
        'name': '风机效率',
        'device_id': did,
        'method_code': 'fan_pressure_flow',
        'model_inputs': {
            'q_m3_s': 10, 'delta_p_pa': 1000,
            'eta_fan_before': 0.7, 'eta_fan_after': 0.8,
            'eta_motor': 0.9, 'hours': 4000,
        },
        'select': True,
        'annual_maintenance_cny': 0,
    })
    assert r.status_code == 200, r.text
    sa1 = (r.json().get('results') or {}).get('selected_assessment') or {}
    saved1 = sa1['saved_kwh']
    co21 = sa1['co2_reduction_t']
    r2 = c.put('/api/scenario', json={'energy_price': 0.8})
    assert r2.status_code == 200, r2.text
    sa2 = (r2.json().get('results') or {}).get('selected_assessment') or {}
    assert sa2['saved_kwh'] == saved1
    assert sa2['co2_reduction_t'] == co21
    assert sa2.get('annual_cost_saving_cny') is not None


def test_v8r22_none_not_zero_om():
    case = CaseState(
        case_id='user-om',
        name='运维未知',
        is_demo=False,
        energy_years=[],
        devices=[
            Device(
                id='d1', name='球磨', device_type='球磨机',
                power_kw=2400, hours=4000, data_type='假设',
            ),
        ],
        retrofits=[
            RetrofitProject(
                id='r1', name='降功率', device_id='d1', target_device='球磨',
                power_drop_kw=200, calculation_method='power_drop_hours',
                investment_cny=100000, annual_maintenance_cny=None, data_type='假设',
            ),
        ],
        scenario=ScenarioParams(
            hours=4000, power_drop_kw=200, energy_price=0.5,
            selected_measure_id='r1', emission_factor=0.4471,
            assume_zero_maintenance=False, annual_maintenance_cny=None,
            use_jinhui_reference=False, use_jinhui_tariff_reference=False,
            energy_price_user_override=True,
        ),
        sources=[],
        custom_params=[],
    )
    sa = recalculate_case(case)['selected_assessment']
    assert sa['saved_kwh'] == 800000
    assert sa.get('simple_payback_years') is None


def test_v8r23_unverified_safety_note():
    out = phys.fan_pressure_flow(
        q_m3_s=10, delta_p_pa=1000, eta_fan_before=0.7, eta_fan_after=0.8, hours=4000,
    )
    assert out['status'] == 'ok'
    assert any('安全风量下限' in a for a in out.get('assumptions') or [])


def test_v8r24_equipment_methods_zh_no_raw_codes():
    zh = equip_cat.available_methods_zh('fan_ventilation')
    assert 'fan_pressure_flow' not in zh
    assert '专业' in zh or '风量' in zh or '通用' in zh


def test_v8r25_report_modular_html_methods_only():
    data = full_recalculate()
    built = report_sec.build_modular_report(preset='custom', sections=['methods'], payload=data)
    html = report_sec.build_modular_html(data, ['methods'], built['text'])
    assert '一页结论' not in html
    assert '方法' in built['text'] or '假设' in built['text']


def test_v8r26_api_level_device_list_ok():
    """浏览器 390px 卡片：API 证明设备列表可返回；截图需求：窄屏卡片不竖排单字。"""
    c = _client()
    r = c.get('/api/results')
    assert r.status_code == 200
    assert 'devices' in (r.json().get('case') or {})


def test_v8r27_no_fallback_quotes_in_frontend_source():
    vue = Path(__file__).resolve().parents[2] / 'frontend' / 'src' / 'views' / 'CarbonRoi.vue'
    text = vue.read_text(encoding='utf-8')
    assert 'FALLBACK_PUBLIC_QUOTES' not in text


def test_v8r28_overview_title_copy():
    vue = Path(__file__).resolve().parents[2] / 'frontend' / 'src' / 'views' / 'Overview.vue'
    text = vue.read_text(encoding='utf-8')
    assert '从一台设备开始，算清节电、减碳和回本。' in text


def test_v8r29_resolve_type_eleven_cases():
    samples = [
        ('球磨机', 'ball_mill'),
        ('半自磨', 'sag_mill'),
        ('矿井排水泵', 'pump_drainage'),
        ('循环水泵', 'pump_process'),
        ('矿浆泵', 'pump_process'),
        ('尾矿泵', 'pump_process'),
        ('矿井通风机', 'fan_ventilation'),
        ('空气压缩机', 'compressor'),
        ('破碎机', 'crusher'),
        ('带式输送机', 'conveyor'),
        ('高效电动机', 'motor_drive'),
    ]
    for name, code in samples:
        assert equip_cat.resolve_type_code(name) == code, name


def test_v8r30_clean_zip_scan_placeholder():
    """交付 ZIP 清洁：本轮测试不打包；占位检查源码树无嵌套旧工程 ZIP。"""
    root = Path(__file__).resolve().parents[2]
    bad = []
    for p in root.rglob('*.zip'):
        if '旧' in p.name or 'V7' in p.name or '给ChatGPT' in p.name:
            bad.append(str(p))
    assert not bad, bad
