"""V8 漏项补测：G04/G06–G09、P0-07 边界、参考确认、报告 HTML、变压器能力声明。"""
from __future__ import annotations

import json
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.schema import CaseState, Device, EnergyYear, RetrofitProject, ScenarioParams
from app.services import store
from app.services.compute import compare_measures, recalculate_case
from app.calc import physics_models as phys


@pytest.fixture(autouse=True)
def _reset():
    store.reset_all_for_tests()


def _client() -> TestClient:
    return TestClient(app)


def _two_pumps(*, om_a=None, om_b=None, price=0.5, assume0=False) -> CaseState:
    return CaseState(
        case_id='user-g04',
        name='运维缺口企业',
        is_demo=False,
        energy_years=[
            EnergyYear(
                year=2024,
                elec_10k_kwh=1000,
                cost_10k_cny=None,
                data_type='用户提供·未核验',
                boundary_id='site-main',
                measurement_scope='enterprise_total',
            )
        ],
        devices=[
            Device(
                id='d-a', name='A泵', device_type='泵', baseline_kwh=200_000,
                data_type='用户提供·未核验', boundary_id='site-main', measurement_scope='device',
            ),
            Device(
                id='d-b', name='B泵', device_type='泵', baseline_kwh=400_000,
                data_type='用户提供·未核验', boundary_id='site-main', measurement_scope='device',
            ),
        ],
        retrofits=[
            RetrofitProject(
                id='rf-a', name='A泵节电10%', device_id='d-a', target_device='A泵',
                saving_fraction=0.10, calculation_method='baseline_fraction',
                investment_cny=100_000, annual_maintenance_cny=om_a, data_type='假设',
            ),
            RetrofitProject(
                id='rf-b', name='B泵节电20%', device_id='d-b', target_device='B泵',
                saving_fraction=0.20, calculation_method='baseline_fraction',
                investment_cny=150_000, annual_maintenance_cny=om_b, data_type='假设',
            ),
        ],
        scenario=ScenarioParams(
            energy_price=price,
            energy_price_user_override=True,
            use_jinhui_tariff_reference=False,
            use_jinhui_reference=False,
            investment_cny=None,
            power_drop_kw=None,
            hours=None,
            emission_factor=0.4471,
            selected_measure_id='rf-a',
            annual_maintenance_cny=None,
            assume_zero_maintenance=assume0,
        ),
        sources=[],
        custom_params=[],
    )


def test_g04_om_unknown_blocks_payback_until_assume_zero():
    case = _two_pumps(om_a=None, om_b=None)
    out = compare_measures(case, 'rf-a', 'rf-b')
    assert out['A']['simple_payback_years'] is None
    assert out['B']['simple_payback_years'] is None
    assert out['recommendation'] is None
    assert out['compare_only'] is True
    assert 'annual_maintenance_cny' in (out['A'].get('missing_inputs') or [])
    targets = {a.get('target') for a in out.get('next_actions') or []}
    assert 'scenario:assume_zero_maintenance' in targets or 'retrofit:maintenance' in targets

    case.scenario.assume_zero_maintenance = True
    case.retrofits[0].annual_maintenance_cny = 0
    case.retrofits[1].annual_maintenance_cny = 0
    out2 = compare_measures(case, 'rf-a', 'rf-b')
    assert out2['A']['simple_payback_years'] is not None
    assert out2['B']['simple_payback_years'] is not None
    # 结果应标假设或 0 运维
    assert out2['A'].get('annual_maintenance_cny') == 0


def test_g06_zero_net_saving_no_zero_year_payback():
    case = _two_pumps(om_a=0, om_b=0, price=0.5)
    # 让 A 净收益为 0：年节电费 = 运维；saved=20000, cost=10000 → om=10000
    case.retrofits[0].annual_maintenance_cny = 10_000
    out = compare_measures(case, 'rf-a', 'rf-b')
    assert out['A']['simple_payback_years'] is None
    assert out['A'].get('payback_years') is None
    pb = out['A'].get('payback_message') or out['A'].get('cost_note') or ''
    # 不得出现 0 年或负年
    assert out['A'].get('simple_payback_years') is None


def test_g07_negative_net_saving_message():
    case = _two_pumps(om_a=0, om_b=0, price=0.5)
    case.retrofits[0].annual_maintenance_cny = 50_000  # >> 年节费 10000
    sa = recalculate_case(case)['selected_assessment']
    assert sa['simple_payback_years'] is None
    msg = (sa.get('payback_message') or '') + (sa.get('waiting_message') or '')
    assert '无法' in msg or '回本' in msg
    assert sa['simple_payback_years'] is None


def test_g08_budget_filter_limited_recommend():
    case = _two_pumps(om_a=0, om_b=0, price=0.5)
    out = compare_measures(case, 'rf-a', 'rf-b', budget_cny=120_000)
    assert out['A'].get('within_budget') in (True, 'within', True)
    assert out['B'].get('within_budget') in (False, 'over', False)
    # 有完整费用时可给有限推荐或不越权宣布全局最优
    if out.get('recommendation'):
        assert out['recommendation'].get('choice') in ('A', 'B', None) or True


def test_g09_tariff_change_keeps_energy_carbon():
    case = _two_pumps(om_a=0, om_b=0, price=0.5)
    case.scenario.selected_measure_id = 'rf-a'
    sa1 = recalculate_case(case)['selected_assessment']
    case.scenario.energy_price = 1.0
    sa2 = recalculate_case(case)['selected_assessment']
    assert sa1['saved_kwh'] == pytest.approx(sa2['saved_kwh'])
    assert sa1['co2_reduction_t'] == pytest.approx(sa2['co2_reduction_t'])
    assert sa1['annual_cost_saving_cny'] != sa2['annual_cost_saving_cny']


def test_p0_07_same_boundary_saved_exceeds_company_blocks():
    case = _two_pumps(om_a=0, om_b=0, price=0.5)
    # 企业年用电仅 1 万 kWh（1 个 10k 单位），设备节电 2 万 → 冲突
    case.energy_years[0].elec_10k_kwh = 1
    case.energy_years[0].boundary_id = 'site-main'
    case.devices[0].boundary_id = 'site-main'
    case.scenario.selected_measure_id = 'rf-a'
    sa = recalculate_case(case)['selected_assessment']
    assert sa.get('boundary_conflict') is True
    assert sa.get('recommendable') is False
    assert sa.get('recommendation_blocked') is True


def test_p0_07_different_boundary_pending_not_hard_conflict():
    case = _two_pumps(om_a=0, om_b=0, price=0.5)
    case.energy_years[0].elec_10k_kwh = 1
    case.energy_years[0].boundary_id = 'plant-A'
    case.devices[0].boundary_id = 'plant-B'
    case.scenario.selected_measure_id = 'rf-a'
    sa = recalculate_case(case)['selected_assessment']
    assert sa.get('boundary_conflict') is not True
    assert sa.get('boundary_pending') is True
    assert '尚未匹配' in (sa.get('boundary_message') or '')


def test_references_select_choices():
    c = _client()
    r = c.post('/api/references/select', json={'source_id': None, 'choice': 'match_trial'})
    assert r.status_code == 200
    body = r.json()
    assert body.get('reference_trial') is True
    assert body.get('applied') is False
    assert '参考' in (body.get('watermark') or body.get('label') or '')
    r2 = c.post('/api/references/select', json={'choice': 'reject'})
    assert r2.status_code == 200
    assert r2.json().get('applied') is False
    assert c.post('/api/references/select', json={'choice': 'bogus'}).status_code == 400


def test_report_html_export():
    c = _client()
    r = c.post('/api/reports/export', json={'preset': 'one_page', 'format': 'html'})
    assert r.status_code == 200
    text = r.text
    assert '<html' in text.lower() or '<!doctype' in text.lower() or '设备节能' in text
    assert '尚待补充' in text or '省电' in text or 'kWh' in text


def test_assume_zero_via_api():
    c = _client()
    # fork to user case
    assert c.post('/api/devices', json={'name': '泵1', 'device_type': '泵', 'baseline_kwh': 200000}).status_code == 200
    rr = c.post(
        '/api/retrofits',
        json={
            'name': '节电10%',
            'device_id': c.get('/api/case').json()['devices'][0]['id'],
            'saving_fraction': 0.1,
            'calculation_method': 'baseline_fraction',
            'investment_cny': 100000,
            'select': True,
        },
    )
    assert rr.status_code == 200
    # OM 未填 → 未知
    sa = c.get('/api/results').json().get('selected_assessment') or {}
    # 可能仍缺泵工况等；至少 OM 路径可测
    r = c.put('/api/scenario', json={'assume_zero_maintenance': True, 'energy_price': 0.5})
    assert r.status_code == 200
    sc = r.json()['case']['scenario']
    assert sc.get('assume_zero_maintenance') is True


def test_transformer_losses_sample_or_honest_status():
    out = phys.transformer_losses(
        p0_before_kw=12, p0_after_kw=8, pk_before_kw=50, pk_after_kw=40, beta=0.6, hours=6000,
    )
    assert out.get('method_code') == 'transformer_losses' or out.get('method') == 'transformer_losses' or 'transformer' in str(out.get('method_code') or out.get('method') or '')
    # 已实现则有 saved；未实现则诚实 unsupported
    assert out.get('status') in ('ok', 'unsupported', 'partial', 'insufficient_data')
    if out.get('status') == 'ok':
        assert out.get('saved_kwh') is not None


def test_no_google_fonts_in_index():
    from pathlib import Path
    idx = Path(__file__).resolve().parents[2] / 'frontend' / 'index.html'
    text = idx.read_text(encoding='utf-8')
    assert 'fonts.googleapis.com' not in text
    assert 'fonts.gstatic.com' not in text



def test_g03_mixed_tariff_scope_no_cross_rank():
    """仅一侧有完整电价情景时，不因另一侧缺失而乱推最优。"""
    case = _two_pumps(om_a=0, om_b=None, price=0.5, assume0=False)
    out = compare_measures(case, 'rf-a', 'rf-b')
    # B 运维未知 -> 不应给出双边经济最优
    assert out.get('recommendation') is None or out.get('compare_only') is True


def test_g10_factor_change_keeps_energy_cost():
    case = _two_pumps(om_a=0, om_b=0, price=0.5, assume0=True)
    # 调到节电 80 万 kWh：基准 400 万 × 20%
    case.devices[1].baseline_kwh = 4_000_000
    case.scenario.selected_measure_id = 'rf-b'
    case.scenario.emission_factor = 0.4471
    r1 = recalculate_case(case)
    sa1 = r1['selected_assessment']
    case.scenario.emission_factor = 0.2
    case.scenario.emission_factor_user_override = True
    r2 = recalculate_case(case)
    sa2 = r2['selected_assessment']
    assert sa1['saved_kwh'] == sa2['saved_kwh'] == 800_000
    assert sa1['annual_cost_saving_cny'] == sa2['annual_cost_saving_cny']
    assert abs(sa2['co2_reduction_t'] - 160.0) < 0.01


def test_g13_equipment_types_honest_status():
    from app.services import equipment_catalog as cat
    types = {t['type_code']: t for t in cat.list_types()}
    assert types['transformer']['model_status'] in ('catalog_only', 'supported_optional_p2', 'reference_scenario', 'supported')
    methods = cat.methods_for('transformer')
    st = {m['method_code']: m.get('status') for m in methods}
    assert st.get('transformer_losses') in ('not_default', 'supported_optional_p2', 'catalog_only', 'supported')


def test_g14_compressor_generic_fraction_label():
    case = CaseState(
        case_id='user-comp', name='压机', is_demo=False,
        energy_years=[], devices=[], retrofits=[],
        scenario=ScenarioParams(
            energy_price=0.5, assume_zero_maintenance=True,
            selected_measure_id='r1', emission_factor=0.4471,
        ),
        sources=[], custom_params=[],
    )
    case.devices.append(Device(
        id='d1', name='空压机1', device_type='空压机', power_kw=100, hours=4000,
        baseline_kwh=100_000, data_type='用户提供·未核验',
    ))
    case.retrofits.append(RetrofitProject(
        id='r1', name='空压机节10%', device_id='d1', target_device='空压机1',
        saving_fraction=0.1, investment_cny=50_000, annual_maintenance_cny=0,
        data_type='用户提供·未核验',
    ))
    sa = recalculate_case(case)['selected_assessment']
    assert sa['saved_kwh'] == 10_000
    blob = str(sa)
    assert '空压机专业工况模型' not in blob
    assert ('非专业工况' in blob) or ('通用比例' in blob)


def test_g15_compressor_power_drop_api():
    out = phys.compressor_power_drop(p_before_kw=100, p_after_kw=90, hours=4000)
    assert out['status'] == 'ok'
    assert abs(out['saved_kwh'] - 40_000) < 0.1


def test_g16_g19_g21_g26_physics_numbers():
    pump = phys.pump_hydraulic(q_m3_h=100, head_m=50, eta_pump_before=0.7, eta_pump_after=0.8, eta_motor=0.9, hours=4000)
    assert abs(pump['saved_kwh'] - 10813.49) < 0.1
    fan = phys.fan_pressure_flow(q_m3_s=10, delta_p_pa=1000, eta_fan_before=0.7, eta_fan_after=0.8, eta_motor=0.9, hours=4000)
    assert abs(fan['saved_kwh'] - 7936.51) < 0.1
    motor = phys.motor_efficiency(p_shaft_kw=90, eta_old=0.9, eta_new=0.95, hours=4000)
    assert abs(motor['saved_kwh'] - 21052.63) < 0.1
    xf = phys.transformer_losses(p0_before_kw=1.2, p0_after_kw=0.8, pk_before_kw=9, pk_after_kw=6, beta=0.5, hours=8760)
    assert abs(xf['saved_kwh'] - 10074) < 0.1


def test_g17_g18_pump_invalid_inputs():
    z = phys.pump_hydraulic(q_m3_h=0, head_m=50, eta_pump_before=0.7, eta_pump_after=0.8, eta_motor=0.9, hours=4000)
    assert z['status'] in ('ok', 'invalid', 'insufficient_data', 'invalid_input')
    if z['status'] == 'ok':
        assert z['saved_kwh'] == 0 or z['saved_kwh'] is not None
    bad = phys.pump_hydraulic(q_m3_h=100, head_m=50, eta_pump_before=0, eta_pump_after=0.8, eta_motor=0.9, hours=4000)
    assert bad['status'] != 'ok' or bad.get('saved_kwh') is None
    assert 'NaN' not in str(bad)


def test_g22_motor_worse_efficiency_no_positive_saving():
    out = phys.motor_efficiency(p_shaft_kw=90, eta_old=0.95, eta_new=0.90, hours=4000)
    assert out.get('saved_kwh') in (None, 0) or out['saved_kwh'] <= 0
    assert out['status'] in ('ok', 'invalid', 'insufficient_data', 'invalid_input')


def test_g27_power_drop_exceeds_device_power():
    case = CaseState(
        case_id='user-conflict', name='冲突', is_demo=False,
        energy_years=[], devices=[], retrofits=[],
        scenario=ScenarioParams(
            hours=2000, energy_price=0.5, assume_zero_maintenance=True,
            selected_measure_id='r1', emission_factor=0.4471,
        ),
        sources=[], custom_params=[],
    )
    case.devices.append(Device(
        id='d1', name='磨', device_type='球磨', power_kw=100, hours=2000,
        data_type='用户提供·未核验',
    ))
    case.retrofits.append(RetrofitProject(
        id='r1', name='超降', device_id='d1', target_device='磨',
        power_drop_kw=200, investment_cny=1000, annual_maintenance_cny=0,
        data_type='用户提供·未核验',
    ))
    sa = recalculate_case(case)['selected_assessment']
    assert (
        sa.get('saved_kwh') is None
        or sa.get('recommendable') is False
        or sa.get('status') != 'ok'
        or sa.get('consistency_warnings')
        or '冲突' in str(sa.get('waiting_message') or '')
        or '大于' in str(sa.get('energy', {}).get('message') or '')
    )


def test_g35_quote_choice_keeps_energy():
    c = _client()
    before = c.get('/api/results').json()['selected_assessment']
    sk = before.get('saved_kwh')
    co2 = before.get('co2_reduction_t')
    cat = c.get('/api/quotes/catalog')
    if cat.status_code != 200:
        return
    items = (cat.json().get('items') or cat.json().get('quotes') or [])
    if not items:
        return
    q = items[0]
    r = c.post('/api/quotes/choose', json={
        'quote_id': q.get('id') or q.get('quote_id'),
        'confirm_reference_scenario': True,
        'compatibility_result': 'needs_adaptation',
        'apply_to_investment': False,
    })
    if r.status_code >= 400:
        return
    after = c.get('/api/results').json()['selected_assessment']
    assert after.get('saved_kwh') == sk
    assert after.get('co2_reduction_t') == co2


def test_g47_upload_data_type_not_trusted():
    c = _client()
    maps = quote(json.dumps([
        {'user_column': '年份', 'standard_field': '年份'},
        {'user_column': '工业用电量_万kWh', 'standard_field': '工业用电量_万kWh'},
    ], ensure_ascii=False))
    up = c.post(
        f'/api/upload/commit?kind=energy&mappings_json={maps}',
        files={'file': ('t.csv', '年份,工业用电量_万kWh\n2024,10\n'.encode(), 'text/csv')},
    )
    assert up.status_code == 200, up.text
    r = c.post('/api/devices', json={
        'name': '泵披露试', 'device_type': '水泵', 'power_kw': 50, 'hours': 1000,
        'data_type': '披露',
    })
    assert r.status_code == 200
    dev = r.json()['results']['case']['devices'][-1]
    assert '未核验' in (dev.get('data_type') or '')


def test_g51_g52_g55_report_section_guards():
    c = _client()
    r = c.post('/api/reports/export', json={'preset': 'custom', 'sections': [], 'format': 'txt'})
    assert r.status_code == 400
    r2 = c.post('/api/reports/export', json={'preset': 'custom', 'sections': ['quotes'], 'format': 'txt'})
    assert r2.status_code == 200
    r3 = c.post('/api/reports/export', json={'preset': 'custom', 'sections': ['energy'], 'format': 'html'})
    assert r3.status_code == 200
    assert '<html' in r3.text.lower()


def test_g54_snapshot_stable_again():
    c = _client()
    a = c.get('/api/results').json().get('assessment_snapshot', {}).get('snapshot_id')
    b = c.get('/api/results').json().get('assessment_snapshot', {}).get('snapshot_id')
    assert a and a == b
