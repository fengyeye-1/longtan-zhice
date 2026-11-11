"""V8 P0 + G01–G20 核心验收。"""
from __future__ import annotations

import csv
import io
import sqlite3
from pathlib import Path
from subprocess import PIPE, run

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.schema import CaseState, Device, EnergyYear, RetrofitProject, ScenarioParams
from app.services import store
from app.services.compute import compare_measures, recalculate_case, render_csv_rows


@pytest.fixture(autouse=True)
def _reset():
    store.reset_all_for_tests()


def _client() -> TestClient:
    return TestClient(app)


def _blank_enterprise_two_pumps() -> CaseState:
    return CaseState(
        case_id='user-test-v8',
        name='测试企业·无电价',
        is_demo=False,
        energy_years=[
            EnergyYear(year=2024, elec_10k_kwh=1000, cost_10k_cny=None, data_type='用户提供·未核验')
        ],
        devices=[
            Device(id='d-a', name='A泵', device_type='泵', baseline_kwh=200_000, data_type='用户提供·未核验'),
            Device(id='d-b', name='B泵', device_type='泵', baseline_kwh=400_000, data_type='用户提供·未核验'),
        ],
        retrofits=[
            RetrofitProject(
                id='rf-a',
                name='A泵节电10%',
                device_id='d-a',
                target_device='A泵',
                saving_fraction=0.10,
                calculation_method='baseline_fraction',
                investment_cny=100_000,
                annual_maintenance_cny=0,
                data_type='假设',
            ),
            RetrofitProject(
                id='rf-b',
                name='B泵节电20%',
                device_id='d-b',
                target_device='B泵',
                saving_fraction=0.20,
                calculation_method='baseline_fraction',
                investment_cny=150_000,
                annual_maintenance_cny=0,
                data_type='假设',
            ),
        ],
        scenario=ScenarioParams(
            energy_price=None,
            energy_price_user_override=False,
            use_jinhui_tariff_reference=False,
            use_jinhui_reference=False,
            investment_cny=None,
            power_drop_kw=None,
            hours=None,
            emission_factor=0.4471,
            selected_measure_id='rf-a',
            annual_maintenance_cny=0,
        ),
        sources=[],
        custom_params=[],
    )


def test_g01_no_tariff_no_economic_recommend():
    out = compare_measures(_blank_enterprise_two_pumps(), 'rf-a', 'rf-b')
    assert out['A']['saved_kwh'] == pytest.approx(20_000, rel=1e-6)
    assert out['B']['saved_kwh'] == pytest.approx(80_000, rel=1e-6)
    assert out['A']['simple_payback_years'] is None
    assert out['B']['simple_payback_years'] is None
    assert out['recommendation'] is None
    assert out['compare_only'] is True
    assert out['can_rank_economic'] is False
    assert any(a.get('target') == 'carbon:tariff' for a in out.get('next_actions') or [])


def test_g02_carbon_ok_without_tariff():
    out = compare_measures(_blank_enterprise_two_pumps(), 'rf-a', 'rf-b')
    assert out['A']['co2_reduction_t'] == pytest.approx(8.942, rel=1e-3)
    assert out['B']['co2_reduction_t'] == pytest.approx(35.768, rel=1e-3)


def test_g05_compressor_uses_generic_fraction_label():
    case = _blank_enterprise_two_pumps()
    case.devices.append(
        Device(id='d-c', name='1#空压机', device_type='空压机', baseline_kwh=100_000, data_type='用户提供·未核验')
    )
    case.retrofits.append(
        RetrofitProject(
            id='rf-c',
            name='空压节电10%',
            device_id='d-c',
            target_device='1#空压机',
            saving_fraction=0.10,
            calculation_method='baseline_fraction',
            investment_cny=50_000,
            data_type='假设',
        )
    )
    case.scenario.selected_measure_id = 'rf-c'
    sa = recalculate_case(case)['selected_assessment']
    assert sa['saved_kwh'] == pytest.approx(10_000, rel=1e-6)
    assert sa['technology_type'] == 'generic_fraction'
    label = sa.get('model_label') or ''
    assert '通用比例' in label
    assert '空压' in label


def test_p0_custom_params_reject_negative():
    c = _client()
    before = c.get('/api/case').json()
    r = c.post(
        '/api/custom-params',
        json={
            'name': '坏电价',
            'unit': '元/kWh',
            'source': 'test',
            'target': 'scenario',
            'value': -1,
            'mapped_model_field': 'energy_price',
        },
    )
    assert r.status_code in (400, 422)
    after = c.get('/api/case').json()
    assert after['scenario']['energy_price'] == before['scenario']['energy_price']
    assert len(after.get('custom_params') or []) == len(before.get('custom_params') or [])


@pytest.mark.parametrize(
    'field,value',
    [
        ('hours', -10),
        ('investment_cny', -100),
        ('emission_factor', -0.4),
        ('pump_baseline_kwh', -100),
    ],
)
def test_p0_custom_params_reject_other_negatives(field, value):
    c = _client()
    r = c.post(
        '/api/custom-params',
        json={
            'name': f'坏-{field}',
            'unit': '-',
            'source': 'test',
            'target': 'scenario',
            'value': value,
            'mapped_model_field': field,
        },
    )
    assert r.status_code in (400, 422)


def test_p0_csv_formula_injection():
    text = render_csv_rows([['字段', '数值'], ['x', '=1+1'], ['y', '+cmd'], ['z', '正常,逗号']])
    body = text.lstrip('\ufeff')
    rows = list(csv.reader(io.StringIO(body)))
    assert rows[1][1].startswith("'")
    assert rows[3][1] == '正常,逗号'


def test_p0_examples_jinhui_no_side_effect():
    c = _client()
    # 通过改示范触发 fork，得到用户项目
    assert c.post('/api/devices', json={'name': '旁路设备', 'device_type': '通用'}).status_code == 200
    active_before = c.get('/api/cases').json()['active']
    assert active_before != 'jinhui-demo'
    r = c.get('/api/examples/jinhui')
    assert r.status_code == 200
    body = r.json()
    # 返回示范结果，但不切换 active
    case = body.get('case') or {}
    assert case.get('is_demo') is True or case.get('case_id') == 'jinhui-demo'
    active_after = c.get('/api/cases').json()['active']
    assert active_after == active_before


def test_p0_snapshot_id_stable():
    c = _client()
    a = c.get('/api/results').json()
    b = c.get('/api/results').json()
    sa = a.get('assessment_snapshot') or {}
    sb = b.get('assessment_snapshot') or {}
    assert sa.get('snapshot_id')
    assert sa.get('snapshot_id') == sb.get('snapshot_id')
    assert sa.get('version_hash') == sb.get('version_hash')


def test_equipment_types_api():
    c = _client()
    r = c.get('/api/equipment/types')
    assert r.status_code == 200
    codes = {t['type_code'] for t in r.json()['types']}
    assert {'compressor', 'ball_mill', 'pump_drainage', 'fan_ventilation'} <= codes


def test_physics_pump_api():
    c = _client()
    r = c.post(
        '/api/equipment/assessments/calculate',
        json={
            'method_code': 'pump_hydraulic',
            'q_m3_h': 100,
            'head_m': 50,
            'eta_pump_before': 0.7,
            'eta_pump_after': 0.8,
            'eta_motor': 0.9,
            'hours': 4000,
        },
    )
    assert r.status_code == 200
    assert r.json()['status'] == 'ok'
    assert r.json()['saved_kwh'] > 0


def test_references_ssrf_blocked():
    c = _client()
    assert c.post('/api/references/fetch', json={'url': 'http://127.0.0.1/secret'}).status_code == 400
    assert c.post('/api/references/fetch', json={'url': 'https://evil.example/x'}).status_code == 400


def test_delivery_safety_detects_projects(tmp_path):
    root = tmp_path / 'pkg'
    (root / 'backend' / 'data').mkdir(parents=True)
    db = root / 'backend' / 'data' / 'projects.sqlite'
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE projects (id TEXT)')
    con.execute('CREATE TABLE versions (id TEXT)')
    con.execute("INSERT INTO projects VALUES ('p1')")
    con.commit()
    con.close()
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'delivery_safety_check.py'
    proc = run(['python3', str(script), str(root)], stdout=PIPE, stderr=PIPE, text=True)
    assert proc.returncode != 0
    assert 'FAIL' in (proc.stdout + proc.stderr)


def test_restore_demo_forbidden_on_user_case():
    c = _client()
    # 健康检查不再创建会话；改用 /api/session 取得工作区身份后再测恢复限制。
    c.get('/api/session')
    sid = c.cookies.get('ltzs_sid')
    assert sid
    tok = store._ctx_session.set(sid)
    try:
        store.empty_enterprise_case('空白企业')
    finally:
        store._ctx_session.reset(tok)
    assert c.get('/api/case').json()['is_demo'] is False
    assert c.get('/api/case').json()['devices'] == []
    rr = c.post('/api/devices/restore-demo-defaults')
    assert rr.status_code == 403


def test_export_csv_escapes():
    c = _client()
    r = c.get('/api/export/csv')
    assert r.status_code == 200
    assert 'text/csv' in r.headers.get('content-type', '')
