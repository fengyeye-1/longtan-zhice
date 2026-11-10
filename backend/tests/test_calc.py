import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.calc.equipment import (
    mill_saving, mill_scenario, pump_saving, pump_scenario,
    COST_INTENSITY_CNY_PER_KWH, MAX_ANNUAL_HOURS, MAX_ANNUAL_HOURS_LEAP,
)
from app.calc.mill_mars import mill_power_kw, EXAMPLE
from app.calc.carbon import estimate_grid_co2_t
from app.calc.roi import simple_payback
from app.calc.retrofit_sim import device_power_saving, aggregate_enterprise_energy


def test_mill_4000():
    x = mill_saving(4000, investment_cny=1_000_000)
    assert x['annual_electricity_saved_kwh'] == 800000
    assert abs(x['scenario_grid_co2_saved_t'] - 357.68) < 0.01
    assert abs(x['scenario_electricity_cost_saved_cny'] - 800000 * COST_INTENSITY_CNY_PER_KWH) < 0.05
    assert x['is_actual_annual_savings'] is False
    assert abs(x['simple_payback_years'] - 2.75) < 0.01


def test_mill_presets():
    assert mill_saving(2000)['annual_electricity_saved_kwh'] == 400000
    assert mill_saving(6000)['annual_electricity_saved_kwh'] == 1200000


def test_invalid_hours():
    try:
        mill_saving(-1)
        assert False
    except ValueError:
        pass
    try:
        mill_saving(9000)
        assert False
    except ValueError:
        pass
    r = mill_scenario(delta_power_kw=200, hours=-1)
    assert r['status'] == 'invalid_input'
    r2 = mill_scenario(delta_power_kw=200, hours=MAX_ANNUAL_HOURS_LEAP + 1)
    assert r2['status'] == 'invalid_input'
    # V6.1深度: 未指定年份时绝对上限为闰年 8784；8761 现为合法


def test_pump():
    assert abs(pump_saving(200000) - 25860) < 1e-6
    try:
        pump_saving(100, -1)
        assert False
    except ValueError:
        pass
    r = pump_scenario(pump_baseline_kwh=200000)
    assert r['status'] == 'ok'
    assert abs(r['saved_kwh'] - 25860) < 0.01
    assert abs(r['co2_reduction_t'] - 11.562) < 0.02


def test_pump_missing():
    r = pump_scenario(pump_baseline_kwh=None)
    assert r['status'] == 'insufficient_data'
    assert r['saved_kwh'] is None


def test_mars():
    p = mill_power_kw(EXAMPLE)
    assert 2100 <= p <= 2380


def test_carbon_and_roi():
    c = estimate_grid_co2_t(800000)
    assert abs(c['co2_t'] - 357.68) < 0.01
    assert c['is_actual_reduction'] is False
    r = simple_payback(1_000_000, 363842.58)
    assert r['simple_payback_years'] == 2.75


def test_device_aggregate_skips_null():
    a = aggregate_enterprise_energy([
        {'name': 'A', 'power_kw': 100, 'hours': 10, 'data_type': '假设'},
        {'name': 'B', 'power_kw': None, 'hours': 20, 'data_type': '假设'},
        {'name': 'C', 'power_kw': 0, 'hours': 0, 'data_type': '假设'},  # 真实零
    ])
    assert a['scenario_total_kwh'] == 1000  # 100*10 + 0*0
    assert a['included_count'] == 2
    assert len(a['skipped']) == 1


def test_device_aggregate():
    a = aggregate_enterprise_energy([
        {'name': 'A', 'power_kw': 100, 'hours': 10, 'data_type': '假设'},
        {'name': 'B', 'power_kw': 50, 'hours': 20, 'data_type': '假设'},
    ])
    assert a['scenario_total_kwh'] == 2000
    assert a['is_company_disclosed_total'] is False


def test_retrofit_mill():
    r = device_power_saving('mill', hours=4000, power_drop_kw=200)
    assert r['status'] == 'ok'
    assert r['saved_kwh'] == 800000
    assert r['before_kwh'] is None
    assert r['after_kwh'] is None


def test_mill_with_baseline():
    r = mill_scenario(
        delta_power_kw=200, hours=4000, baseline_power_kw=2400,
        investment_cny=1_000_000,
    )
    assert r['status'] == 'ok'
    assert r['before_kwh'] == 9_600_000
    assert r['after_kwh'] == 8_800_000
    assert r['saved_kwh'] == 800_000


def test_tariff_only_changes_money():
    a = mill_scenario(delta_power_kw=200, hours=4000, tariff_cny_per_kwh=0.4, investment_cny=1e6)
    b = mill_scenario(delta_power_kw=200, hours=4000, tariff_cny_per_kwh=0.5, investment_cny=1e6)
    assert a['saved_kwh'] == b['saved_kwh']
    assert a['co2_reduction_t'] == b['co2_reduction_t']
    assert a['annual_cost_saving_cny'] != b['annual_cost_saving_cny']


def test_factor_only_changes_co2():
    a = mill_scenario(delta_power_kw=200, hours=4000, ef_kgco2_per_kwh=0.4)
    b = mill_scenario(delta_power_kw=200, hours=4000, ef_kgco2_per_kwh=0.5)
    assert a['saved_kwh'] == b['saved_kwh']
    assert a['annual_cost_saving_cny'] == b['annual_cost_saving_cny']
    assert a['co2_reduction_t'] != b['co2_reduction_t']


def test_investment_only_payback():
    a = mill_scenario(delta_power_kw=200, hours=4000, investment_cny=500_000)
    b = mill_scenario(delta_power_kw=200, hours=4000, investment_cny=2_000_000)
    assert a['saved_kwh'] == b['saved_kwh']
    assert a['co2_reduction_t'] == b['co2_reduction_t']
    assert a['simple_payback_years'] != b['simple_payback_years']


def test_no_investment_payback_message():
    r = mill_scenario(delta_power_kw=200, hours=4000, investment_cny=None)
    assert r['status'] == 'ok'
    assert r['saved_kwh'] == 800000
    assert r['simple_payback_years'] is None
    assert r['payback_message'] == '待输入投资'


def test_om_above_saving():
    r = mill_scenario(
        delta_power_kw=200, hours=4000,
        investment_cny=1_000_000,
        annual_extra_om_cny=1_000_000,
    )
    assert r['status'] == 'ok'
    assert r['simple_payback_years'] is None
    assert '无法依靠节能回本' in (r['payback_message'] or '')


def test_negative_investment():
    r = mill_scenario(delta_power_kw=200, hours=4000, investment_cny=-1)
    assert r['status'] == 'invalid_input'
