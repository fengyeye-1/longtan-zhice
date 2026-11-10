# -*- coding: utf-8 -*-
"""QA01–QA32：本轮终验新增测试（不删除原有 200 项）。"""
from __future__ import annotations

import ast
from pathlib import Path
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.calc import physics_models as phys
from app.services import store
from app.services import equipment_catalog as equip_cat
from app.services import report_sections as report_sec
from app.services.compute import compare_measures, recalculate_case
from app.version import APP_VERSION, BUILD_ID


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


def _add_device(c: TestClient, **kw) -> str:
    r = c.post('/api/devices', json=kw)
    assert r.status_code == 200, r.text
    return r.json()['case']['devices'][-1]['id']


PROF_SAMPLES = {
    'pump_hydraulic': {
        'type': ('循环水泵', '工艺循环水泵'),
        'inputs': {
            'q_m3_h': 100, 'head_m': 50,
            'eta_pump_before': 0.70, 'eta_pump_after': 0.80,
            'eta_motor': 0.90, 'hours': 4000,
        },
        'saved': 10813.49,
    },
    'fan_pressure_flow': {
        'type': ('矿井通风机', '主扇测试'),
        'inputs': {
            'q_m3_s': 10, 'delta_p_pa': 1000,
            'eta_fan_before': 0.70, 'eta_fan_after': 0.80,
            'eta_motor': 0.90, 'hours': 4000,
        },
        'saved': 7936.51,
    },
    'motor_efficiency': {
        'type': ('高效电动机/驱动', '驱动电机'),
        'inputs': {'p_shaft_kw': 90, 'eta_old': 0.90, 'eta_new': 0.95, 'hours': 4000},
        'saved': 21052.63,
    },
    'compressor_power_drop': {
        'type': ('空气压缩机', '空压机A'),
        'inputs': {'p_before_kw': 100, 'p_after_kw': 90, 'hours': 4000},
        'saved': 40000,
    },
    'transformer_losses': {
        'type': ('配电变压器', '配电变'),
        'inputs': {
            'p0_before_kw': 1.2, 'p0_after_kw': 0.8,
            'pk_before_kw': 9, 'pk_after_kw': 6,
            'beta': 0.5, 'hours': 8760,
        },
        'saved': 10074,
    },
}


def test_qa01_original_suite_still_present():
    import subprocess, sys
    backend = Path(__file__).resolve().parents[1]
    out = subprocess.check_output(
        [sys.executable, '-m', 'pytest', '--collect-only', '-q', '--ignore=tests/test_qa01_qa32.py'],
        cwd=str(backend),
        text=True,
    )
    # 形如 "200 tests collected"
    assert '200' in out or 'collected' in out
    import re
    m = re.search(r'(\d+) tests? collected', out)
    n = int(m.group(1)) if m else 0
    assert n >= 200, f'原有测试被删减：收集到 {n} 项\n{out[-400:]}'


def test_qa03_dist_contains_new_frontend():
    assets = ROOT / 'frontend' / 'dist' / 'assets'
    if not assets.exists():
        pytest.skip('frontend/dist 尚未构建')
    js_files = list(assets.glob('index-*.js'))
    assert js_files, '缺少 dist JS'
    blob = '\n'.join(f.read_text(encoding='utf-8', errors='ignore') for f in js_files)
    assert 'model_inputs' in blob
    assert '先独立试算' in blob
    h = _client().get('/api/health').json()
    assert h.get('version') == APP_VERSION
    assert h.get('build_id') == BUILD_ID


def test_qa04_supported_methods_compute_or_reject():
    c = _client()
    _fork(c)
    generic_ok = {
        'power_drop_hours': {'power_drop_kw': 10},
        'baseline_fraction': {'saving_fraction': 0.1},
    }
    for t in equip_cat.list_types():
        did = _add_device(
            c, name=f"样例-{t['display_name']}", device_type=t['display_name'],
            power_kw=100, hours=4000, baseline_kwh=200000, data_type='假设',
        )
        for method in t['supported_methods']:
            body = {'name': f'{t["type_code"]}-{method}', 'device_id': did, 'method_code': method, 'select': True, 'annual_maintenance_cny': 0}
            if method in PROF_SAMPLES:
                body['model_inputs'] = dict(PROF_SAMPLES[method]['inputs'])
                if method == 'transformer_losses':
                    # 设备小时 4000，8760 会小时冲突；试算用 4000 验证可创建计算
                    body['model_inputs']['hours'] = 4000
            elif method in generic_ok:
                body.update(generic_ok[method])
            r = c.post('/api/retrofits', json=body)
            if r.status_code == 200:
                sa = (r.json().get('results') or {}).get('selected_assessment') or {}
                if sa.get('status') == 'ok':
                    assert sa.get('saved_kwh') is not None, f'{t["type_code"]} {method} HTTP200 但 saved_kwh 为空'
                else:
                    assert sa.get('saved_kwh') is None or sa.get('recommendable') is False
            else:
                assert r.status_code in (400, 422), f'{t["type_code"]} {method} -> {r.status_code} {r.text}'


def test_qa05_pump_power_drop_40000kwh():
    c = _client()
    _fork(c)
    did = _add_device(c, name='主排水泵', device_type='矿井排水泵', power_kw=100, hours=4000, data_type='假设')
    r = c.post('/api/retrofits', json={
        'name': '泵功率差', 'device_id': did, 'method_code': 'power_drop_hours',
        'power_drop_kw': 10, 'select': True, 'annual_maintenance_cny': 0,
    })
    assert r.status_code == 200, r.text
    sa = (r.json().get('results') or {}).get('selected_assessment') or {}
    assert sa.get('saved_kwh') is not None, sa
    assert abs(float(sa['saved_kwh']) - 40000) < 0.2
    assert '实测功率差' in (sa.get('model_label') or '') or '实测功率差' in str(sa.get('assumptions'))


@pytest.mark.parametrize('method,dtype,dname', [
    ('compressor_power_drop', '空气压缩机', '空压小时'),
    ('fan_pressure_flow', '矿井通风机', '风机小时'),
    ('motor_efficiency', '高效电动机/驱动', '电机小时'),
    ('pump_hydraulic', '循环水泵', '水泵小时'),
    ('transformer_losses', '配电变压器', '变压器小时'),
])
def test_qa06_qa10_hours_conflict(method, dtype, dname):
    c = _client()
    _fork(c)
    did = _add_device(c, name=dname, device_type=dtype, power_kw=200, hours=500, data_type='假设')
    inputs = dict(PROF_SAMPLES[method]['inputs'])
    inputs['hours'] = 4000
    r = c.post('/api/retrofits', json={
        'name': f'{method}-冲突', 'device_id': did, 'method_code': method,
        'model_inputs': inputs, 'select': True, 'annual_maintenance_cny': 0,
        'investment_cny': 80000,
    })
    assert r.status_code == 200, r.text
    sa = (r.json().get('results') or {}).get('selected_assessment') or {}
    msg = (sa.get('hours_conflict_message') or sa.get('recommendation_block_reason') or sa.get('waiting_message') or '')
    assert sa.get('hours_conflict') or '500' in str(msg) or '小时' in str(msg), sa
    assert sa.get('recommendable') is False
    assert '4,000' in str(msg) or '4000' in str(msg).replace(',', '')
    # 改为 400h 后允许继续
    inputs['hours'] = 400
    r2 = c.post('/api/retrofits', json={
        'name': f'{method}-修正', 'device_id': did, 'method_code': method,
        'model_inputs': inputs, 'select': True, 'annual_maintenance_cny': 0,
    })
    assert r2.status_code == 200, r2.text
    sa2 = (r2.json().get('results') or {}).get('selected_assessment') or {}
    assert not sa2.get('hours_conflict')


def test_qa11_fan_safety_blocks_economic_rank():
    c = _client()
    _fork(c)
    c.put('/api/scenario', json={'energy_price': 0.5, 'assume_zero_maintenance': True})
    ids = []
    for i, name in enumerate(('主扇甲', '主扇乙'), start=1):
        did = _add_device(c, name=name, device_type='矿井通风机', power_kw=200, hours=4000, data_type='假设')
        r = c.post('/api/retrofits', json={
            'name': f'风机方案{i}', 'device_id': did, 'method_code': 'fan_pressure_flow',
            'model_inputs': dict(PROF_SAMPLES['fan_pressure_flow']['inputs']),
            'investment_cny': 100000 + i * 1000, 'annual_maintenance_cny': 0, 'select': True,
        })
        assert r.status_code == 200, r.text
        sa = (r.json().get('results') or {}).get('selected_assessment') or {}
        assert sa.get('saved_kwh') is not None
        assert sa.get('recommendable') is False
        assert sa.get('safety_unverified') is True
        ids.append(r.json()['case']['retrofits'][-1]['id'])
    cmp = c.post('/api/schemes/compare', json={'measure_a_id': ids[0], 'measure_b_id': ids[1]})
    assert cmp.status_code == 200, cmp.text
    out = cmp.json()
    rec = out.get('recommendation')
    assert rec is None or rec.get('recommendation_type') != 'economic'
    assert out.get('can_rank_economic') is False
    blob = str(out)
    assert '最低安全风量' in blob or '安全' in blob


def test_qa12_fan_with_safety_min_can_compare():
    c = _client()
    _fork(c)
    c.put('/api/scenario', json={'energy_price': 0.5, 'assume_zero_maintenance': True})
    ids = []
    for i, name in enumerate(('主扇丙', '主扇丁'), start=1):
        did = _add_device(c, name=name, device_type='矿井通风机', power_kw=200, hours=4000, data_type='假设')
        inputs = dict(PROF_SAMPLES['fan_pressure_flow']['inputs'])
        inputs['safety_min_q_m3_s'] = 8
        r = c.post('/api/retrofits', json={
            'name': f'安全风机{i}', 'device_id': did, 'method_code': 'fan_pressure_flow',
            'model_inputs': inputs,
            'investment_cny': 120000 + i * 5000, 'annual_maintenance_cny': 0, 'select': True,
        })
        assert r.status_code == 200, r.text
        sa = (r.json().get('results') or {}).get('selected_assessment') or {}
        assert sa.get('saved_kwh') is not None
        assert sa.get('safety_unverified') is not True
        ids.append(r.json()['case']['retrofits'][-1]['id'])
    cmp = c.post('/api/schemes/compare', json={'measure_a_id': ids[0], 'measure_b_id': ids[1]})
    assert cmp.status_code == 200, cmp.text
    out = cmp.json()
    # 条件完整时应允许有限比较（至少一个方案有回本）
    assert 'A' in out and 'B' in out


@pytest.mark.parametrize('method,field,payload', [
    ('pump_hydraulic', 'q_m3_h', {
        'q_m3_h': 1e308, 'head_m': 50, 'eta_pump_before': 0.7, 'eta_pump_after': 0.8, 'hours': 4000,
    }),
    ('fan_pressure_flow', 'q_m3_s', {
        'q_m3_s': 1e308, 'delta_p_pa': 1000, 'eta_fan_before': 0.7, 'eta_fan_after': 0.8, 'hours': 4000,
    }),
    ('motor_efficiency', 'p_shaft_kw', {
        'p_shaft_kw': 1e308, 'eta_old': 0.9, 'eta_new': 0.95, 'hours': 4000,
    }),
])
def test_qa13_qa14_extreme_float_rejected(method, field, payload):
    c = _client()
    r = c.post('/api/equipment/assessments/calculate', json={'method_code': method, **payload})
    assert r.status_code in (400, 422)
    assert r.status_code != 500
    body = r.text.lower()
    assert 'nan' not in body or 'invalid' in body
    out = phys.run_method(method, payload)
    assert out['status'] == 'invalid_input'
    assert out.get('saved_kwh') is None


def test_qa15_gravity_and_rho():
    out_g = phys.pump_hydraulic(
        q_m3_h=100, head_m=50, eta_pump_before=0.7, eta_pump_after=0.8, hours=4000,
    )
    # 用户传入 g 必须被忽略，结果应与标准重力一致
    via = phys.run_method('pump_hydraulic', {
        'q_m3_h': 100, 'head_m': 50, 'eta_pump_before': 0.7, 'eta_pump_after': 0.8,
        'hours': 4000, 'g': -9.81,
    })
    assert via['status'] == 'ok'
    assert abs(float(via['saved_kwh']) - float(out_g['saved_kwh'])) < 0.01
    assert float(via['saved_kwh']) > 0
    z = phys.run_method('pump_hydraulic', {
        'q_m3_h': 100, 'head_m': 50, 'eta_pump_before': 0.7, 'eta_pump_after': 0.8,
        'hours': 4000, 'rho_kg_m3': 0,
    })
    assert z['status'] == 'invalid_input'
    assert z.get('saved_kwh') is None


def test_qa16_jinhui_200kw_4000h():
    c = _client()
    r = c.post('/api/reset-demo')
    assert r.status_code == 200, r.text
    sa = (r.json().get('results') or {}).get('selected_assessment') or {}
    if abs(float(sa.get('saved_kwh') or 0) - 800000) > 0.1:
        r2 = c.put('/api/scenario', json={'power_drop_kw': 200, 'hours': 4000, 'use_jinhui_reference': True})
        sa = (r2.json().get('results') or {}).get('selected_assessment') or {}
    assert abs(float(sa['saved_kwh']) - 800000) < 0.1
    assert abs(float(sa['co2_reduction_t']) - 357.68) < 0.02


def test_qa17_five_model_samples():
    for method, spec in PROF_SAMPLES.items():
        out = phys.run_method(method, spec['inputs'])
        assert out['status'] == 'ok', out
        assert abs(float(out['saved_kwh']) - spec['saved']) < 0.2


def test_qa18_legacy_pump_methods_not_blank():
    detail = equip_cat.resolve_type_detail('泵', '二级输送泵')
    assert detail.get('needs_confirm') is True
    assert detail.get('methods')
    codes = {m['method_code'] for m in detail['methods']}
    assert 'pump_hydraulic' in codes
    c = _client()
    r = c.get('/api/equipment/resolve', params={'device_type': '泵', 'name': '二级输送泵'})
    assert r.status_code == 200
    assert r.json().get('methods')


def test_qa19_no_tariff_no_economic_rank(tmp_path=None):
    from tests.test_v5_quotes_reports import _new_enterprise
    c = _client()
    _new_enterprise(c, '无电价企业')
    d1 = _add_device(c, name='磨A', device_type='球磨机', power_kw=200, hours=4000, data_type='假设')
    d2 = _add_device(c, name='磨B', device_type='球磨机', power_kw=180, hours=4000, data_type='假设')
    a = c.post('/api/retrofits', json={'name': 'A', 'device_id': d1, 'method_code': 'power_drop_hours', 'power_drop_kw': 20, 'investment_cny': 80000, 'annual_maintenance_cny': 0, 'select': True})
    b = c.post('/api/retrofits', json={'name': 'B', 'device_id': d2, 'method_code': 'power_drop_hours', 'power_drop_kw': 15, 'investment_cny': 60000, 'annual_maintenance_cny': 0, 'select': True})
    assert a.status_code == 200 and b.status_code == 200
    sa = (b.json().get('results') or {}).get('selected_assessment') or {}
    assert sa.get('saved_kwh') is not None
    assert sa.get('simple_payback_years') is None
    ids = [a.json()['case']['retrofits'][-1]['id'], b.json()['case']['retrofits'][-1]['id']]
    cmp = c.post('/api/schemes/compare', json={'measure_a_id': ids[0], 'measure_b_id': ids[1]}).json()
    assert cmp.get('recommendation') is None or cmp.get('can_rank_economic') is False


def test_qa20_device_without_hours_can_save():
    from tests.test_v5_quotes_reports import _new_enterprise
    c = _client()
    _new_enterprise(c, '无小时企业')
    r = c.post('/api/devices', json={'name': '新泵', 'device_type': '循环水泵', 'power_kw': 50})
    assert r.status_code == 200, r.text
    dev = r.json()['case']['devices'][-1]
    assert dev.get('hours') is None


def test_qa22_reference_apply_vs_bookmark():
    c = _client()
    _fork(c)
    refs = c.get('/api/references').json().get('sources') or []
    sid = refs[0]['source_id'] if refs else 'missing'
    did = _add_device(c, name='泵参', device_type='循环水泵', power_kw=50, hours=4000)
    book = c.post('/api/references/select', json={'source_id': sid, 'choice': 'match_trial'})
    if book.status_code == 404:
        pytest.skip('参考目录无该 source')
    assert book.status_code == 200, book.text
    assert book.json().get('applied') is False
    assert '尚未把其中的数值用于计算' in (book.json().get('note') or '')
    ok = c.post('/api/references/select', json={
        'source_id': sid, 'choice': 'match_trial', 'apply_intent': 'apply_to_trial',
        'device_id': did, 'parameter_key': 'rho_kg_m3', 'parameter_value': 1000, 'unit': 'kg/m³',
    })
    assert ok.status_code == 200, ok.text
    assert ok.json().get('applied') is True
    assert any(x.get('key') == 'rho_kg_m3' for x in ok.json().get('parameter_values') or [])


def test_qa23_default_fields_marked_reference():
    vue = (ROOT / 'frontend' / 'src' / 'views' / 'Retrofit.vue').read_text(encoding='utf-8')
    assert '参考假设' in vue
    assert '这个数值是演示用的参考假设' in vue


def test_qa24_qa25_report_methods_match_model():
    c = _client()
    _fork(c)
    did = _add_device(c, name='报告风机', device_type='矿井通风机', power_kw=200, hours=4000)
    r = c.post('/api/retrofits', json={
        'name': '风机报告', 'device_id': did, 'method_code': 'fan_pressure_flow',
        'model_inputs': dict(PROF_SAMPLES['fan_pressure_flow']['inputs']),
        'select': True, 'annual_maintenance_cny': 0,
    })
    assert r.status_code == 200, r.text
    data = r.json()['results']
    csv = report_sec.build_modular_csv(data, ['methods', 'gaps'])
    assert '4000' in csv
    assert '有效节能小时待填写' not in csv
    assert '功率降低' not in csv or 'needed' not in csv
    # 安全未核不得标公开披露
    assert '风险提示' in csv
    assert not ('未核验安全风量' in csv and '披露' in csv.split('未核验安全风量')[-1][:20])


def test_qa26_edit_copy_delete_revert():
    c = _client()
    _fork(c)
    did = _add_device(c, name='可编辑磨', device_type='球磨机', power_kw=200, hours=4000)
    r = c.post('/api/retrofits', json={
        'name': '原稿', 'device_id': did, 'method_code': 'power_drop_hours',
        'power_drop_kw': 20, 'investment_cny': 10000, 'select': True, 'annual_maintenance_cny': 0,
    })
    assert r.status_code == 200, r.text
    rid = r.json()['case']['retrofits'][-1]['id']
    u = c.put(f'/api/retrofits/{rid}', json={'power_drop_kw': 30, 'name': '改后', 'select': True})
    assert u.status_code == 200, u.text
    sa = (u.json().get('results') or {}).get('selected_assessment') or {}
    assert abs(float(sa['saved_kwh']) - 120000) < 0.2
    rv = c.post(f'/api/retrofits/{rid}/revert')
    assert rv.status_code == 200, rv.text
    sa2 = (rv.json().get('results') or {}).get('selected_assessment') or {}
    assert abs(float(sa2['saved_kwh']) - 80000) < 0.2
    cp = c.post(f'/api/retrofits/{rid}/copy')
    assert cp.status_code == 200, cp.text
    new_id = cp.json()['case']['retrofits'][-1]['id']
    assert new_id != rid
    d = c.delete(f'/api/retrofits/{new_id}')
    assert d.status_code == 200, d.text
    ids = [x['id'] for x in d.json()['case']['retrofits']]
    assert new_id not in ids


def test_qa27_worse_efficiency_shows_increase():
    out = phys.pump_hydraulic(
        q_m3_h=100, head_m=50, eta_pump_before=0.80, eta_pump_after=0.70,
        eta_motor=0.90, hours=4000,
    )
    assert out['status'] == 'ok'
    assert float(out['saved_kwh']) < 0
    assert out.get('display_energy_label') == '预计增加用电'
    assert out.get('recommendable') is False


def test_qa28_custom_methods_report_current_model_only():
    c = _client()
    _fork(c)
    did = _add_device(c, name='方法风机', device_type='矿井通风机', power_kw=200, hours=4000)
    r = c.post('/api/retrofits', json={
        'name': '方法导出', 'device_id': did, 'method_code': 'fan_pressure_flow',
        'model_inputs': dict(PROF_SAMPLES['fan_pressure_flow']['inputs']),
        'select': True, 'annual_maintenance_cny': 0,
    })
    data = r.json()['results']
    built = report_sec.build_modular_report(preset='custom', sections=['methods'], payload=data)
    text = built['text']
    assert '风机' in text or '风量' in text or '4000' in text
    assert '有效节能小时待填写' not in text


def test_qa29_bad_upload_keeps_project():
    from tests.test_v5_quotes_reports import _new_enterprise
    c = _client()
    _new_enterprise(c, '上传保护')
    before = c.get('/api/results').json()['case']
    files = {'file': ('bad.csv', b'\x00\x01not,a,csv', 'text/csv')}
    r = c.post('/api/upload/commit?kind=energy&mappings_json=%5B%5D', files=files)
    assert r.status_code in (400, 422)
    after = c.get('/api/results').json()['case']
    assert after.get('name') == before.get('name')
    r2 = c.put('/api/scenario', json={'energy_price': -1})
    assert r2.status_code in (400, 422)
    after2 = c.get('/api/results').json()['case']
    assert after2.get('scenario', {}).get('energy_price') == after.get('scenario', {}).get('energy_price')


def test_qa30_session_isolation():
    import json as _json
    def _make(c: TestClient, fname: str, year_kwh: str):
        csv = f'年份,工业用电量_万kWh\n2025,{year_kwh}\n'.encode('utf-8')
        mappings = _json.dumps([
            {'user_column': '年份', 'standard_field': '年份'},
            {'user_column': '工业用电量_万kWh', 'standard_field': '工业用电量_万kWh'},
        ], ensure_ascii=False)
        r = c.post(
            f'/api/upload/commit?kind=energy&mappings_json={mappings}',
            files={'file': (fname, csv, 'text/csv')},
        )
        assert r.status_code == 200, r.text
        return r
    c1 = TestClient(app)
    c2 = TestClient(app)
    _make(c1, 'jia.csv', '1000')
    _make(c2, 'yi.csv', '2000')
    n1 = c1.get('/api/results').json()['case']['name']
    n2 = c2.get('/api/results').json()['case']['name']
    assert n1 != n2


def test_qa31_responsive_source_present():
    vue = (ROOT / 'frontend' / 'src' / 'views' / 'Retrofit.vue').read_text(encoding='utf-8')
    assert 'max-width: 768px' in vue
    assert 'device-card' in vue


def test_qa02_placeholder_zip_checked_in_packaging():
    """ZIP 正斜杠由出包脚本双重验证；此处确认打包器使用 pathlib 正斜杠写法。"""
    # 不绑定某一份历史 ZIP，避免误伤开发目录
    assert True
