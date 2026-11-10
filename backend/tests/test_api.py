import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from app.main import app
from app.services import store

client = TestClient(app)


def setup_function():
    store.reset_all_for_tests()


def test_health():
    r = client.get('/api/health')
    assert r.status_code == 200


def test_t01_jinhui_default_nonzero():
    """T01: 默认 200kW×4000h → 800000 / 357.68，不得全 0"""
    store.reset_all_for_tests()
    r = client.get('/api/results')
    assert r.status_code == 200
    data = r.json()
    assert data['case']['case_id'] == 'jinhui-demo'
    mill = data['mill_scenario']
    assert mill['status'] == 'ok'
    assert mill['saved_kwh'] == 800000
    assert abs(mill['co2_reduction_t'] - 357.68) < 0.01
    assert abs(mill['annual_cost_saving_cny'] - 363842.58) < 0.5
    assert abs(mill['simple_payback_years'] - 2.75) < 0.01
    # before/after 无基准 → null 不是 0
    assert mill['before_kwh'] is None
    assert mill['after_kwh'] is None
    assert data['before_after']['before_kwh'] is None
    assert data['before_after']['after_kwh'] is None
    assert data['before_after']['before_scenario_kwh'] is None
    assert '尚无基准功率' in data['before_after']['display_before']
    assert data['features']['fake_hourly_forbidden'] is True
    # 首页不再塞调试缺失列表
    assert data['missing_fields'] == []
    assert any(i['key'] == 'hours' for i in data['data_prep'])


def test_t02_hours_presets():
    store.reset_all_for_tests()
    r = client.put('/api/scenario', json={'hours': 2000})
    assert r.json()['results']['mill_scenario']['saved_kwh'] == 400000
    store.reset_all_for_tests()
    r = client.put('/api/scenario', json={'hours': 6000})
    assert r.json()['results']['mill_scenario']['saved_kwh'] == 1200000


def test_t03_no_baseline_shows_pending():
    store.reset_all_for_tests()
    data = client.get('/api/results').json()
    assert data['before_after']['before_kwh'] is None
    assert data['mill_scenario']['saved_kwh'] == 800000
    assert data['mill_scenario']['co2_reduction_t'] is not None


def test_t04_demo_baseline_2400():
    store.reset_all_for_tests()
    r = client.put('/api/scenario', json={'use_demo_baseline': True})
    ba = r.json()['results']['before_after']
    mill = r.json()['results']['mill_scenario']
    assert ba['before_kwh'] == 9_600_000
    assert ba['after_kwh'] == 8_800_000
    assert mill['saved_kwh'] == 800_000


def test_t05_clear_investment():
    store.reset_all_for_tests()
    r = client.put('/api/scenario', json={'clear_investment_cny': True})
    mill = r.json()['results']['mill_scenario']
    assert mill['saved_kwh'] == 800000
    assert mill['co2_reduction_t'] is not None
    assert mill['annual_cost_saving_cny'] is not None
    assert mill['simple_payback_years'] is None
    assert mill['payback_message'] == '待输入投资'


def test_t06_payback_275():
    store.reset_all_for_tests()
    mill = client.get('/api/results').json()['mill_scenario']
    assert abs(mill['annual_cost_saving_cny'] - 363842.58) < 0.5
    assert abs(mill['simple_payback_years'] - 2.75) < 0.01


def test_t07_tariff_only_money():
    store.reset_all_for_tests()
    base = client.get('/api/results').json()['mill_scenario']
    r = client.put('/api/scenario', json={'energy_price': 0.5})
    mill = r.json()['results']['mill_scenario']
    assert mill['saved_kwh'] == base['saved_kwh']
    assert mill['co2_reduction_t'] == base['co2_reduction_t']
    assert mill['annual_cost_saving_cny'] != base['annual_cost_saving_cny']


def test_t08_factor_only_co2():
    store.reset_all_for_tests()
    base = client.get('/api/results').json()['mill_scenario']
    r = client.put('/api/scenario', json={'emission_factor': 0.5})
    mill = r.json()['results']['mill_scenario']
    assert mill['saved_kwh'] == base['saved_kwh']
    assert mill['annual_cost_saving_cny'] == base['annual_cost_saving_cny']
    assert mill['co2_reduction_t'] != base['co2_reduction_t']


def test_t09_pump_missing_no_block_mill():
    store.reset_all_for_tests()
    data = client.get('/api/results').json()
    assert data['pump_scenario']['status'] == 'insufficient_data'
    assert data['mill_scenario']['status'] == 'ok'
    assert data['mill_scenario']['saved_kwh'] == 800000


def test_t10_pump_200000():
    store.reset_all_for_tests()
    r = client.put('/api/scenario', json={'pump_baseline_kwh': 200000})
    pump = r.json()['results']['pump_scenario']
    assert pump['status'] == 'ok'
    assert abs(pump['saved_kwh'] - 25860) < 0.01
    assert abs(pump['co2_reduction_t'] - 11.56) < 0.02


def test_t11_negative_hours_and_limit():
    store.reset_all_for_tests()
    r = client.put('/api/scenario', json={'hours': -1})
    assert r.status_code == 400
    store.reset_all_for_tests()
    r = client.put('/api/scenario', json={'hours': 9000})
    assert r.status_code == 400
    store.reset_all_for_tests()
    r = client.put('/api/scenario', json={'investment_cny': -100})
    assert r.status_code == 400


def test_t14_reset_demo():
    store.reset_all_for_tests()
    r0 = client.put('/api/scenario', json={'hours': 2000})
    assert r0.json()['case']['case_id'] != 'jinhui-demo'
    r = client.post('/api/reset-demo')
    assert r.status_code == 200
    assert r.json()['results']['case']['case_id'] == 'jinhui-demo' or r.json()['case']['case_id'] == 'jinhui-demo'
    assert r.json()['results']['mill_scenario']['saved_kwh'] == 800000


def test_t15_export_report():
    store.reset_all_for_tests()
    r = client.get('/api/export/report')
    assert r.status_code == 200
    assert '800000' in r.text or '800,000' in r.text or '节电' in r.text
    assert '来源' in r.text
    assert '假设' in r.text or '情景' in r.text


def test_t16_om_above_saving():
    store.reset_all_for_tests()
    r = client.put('/api/scenario', json={'annual_maintenance_cny': 1_000_000})
    mill = r.json()['results']['mill_scenario']
    assert mill['simple_payback_years'] is None
    assert '无法依靠节能回本' in (mill['payback_message'] or '')


def test_change_hours_forks():
    store.reset_all_for_tests()
    r = client.put('/api/scenario', json={'hours': 2000})
    assert r.status_code == 200
    assert r.json()['results']['mill_scenario']['saved_kwh'] == 400000
    assert r.json()['case']['case_id'] != 'jinhui-demo'


def test_add_device_updates_energy():
    store.reset_all_for_tests()
    r = client.post('/api/devices', json={
        'name': '新球磨', 'device_type': '球磨', 'power_kw': 100, 'hours': 1000
    })
    assert r.status_code == 200
    ent = r.json()['results']['enterprise_devices']
    assert ent['scenario_total_kwh'] == 100000


def test_reject_bad_upload():
    store.reset_all_for_tests()
    content = 'foo,bar\n1,2\n'.encode('utf-8')
    r = client.post(
        '/api/upload/commit?kind=energy&mappings_json=%5B%5D',
        files={'file': ('bad.csv', content, 'text/csv')},
    )
    assert r.status_code == 400
    assert client.get('/api/results').json()['case']['case_id'] == 'jinhui-demo'


def test_upload_energy_recalc():
    store.reset_all_for_tests()
    csv = (
        '年份,工业用电量_万kWh,电力费用_万元\n'
        '2024,5000,2000\n'
        '2025,6000,2500\n'
    ).encode('utf-8')
    mappings = '[{"user_column":"年份","standard_field":"年份"},{"user_column":"工业用电量_万kWh","standard_field":"工业用电量_万kWh"},{"user_column":"电力费用_万元","standard_field":"电力费用_万元"}]'
    r = client.post(
        f'/api/upload/commit?kind=energy&mappings_json={mappings}',
        files={'file': ('t.csv', csv, 'text/csv')},
    )
    assert r.status_code == 200
    body = r.json()
    assert body['accepted'] is True
    assert body['results']['case']['case_id'] != 'jinhui-demo'
    assert len(body['results']['energy_summary']) == 2
