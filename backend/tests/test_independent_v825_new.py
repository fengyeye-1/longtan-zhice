"""V8.2.5 独立新增回归：仅限隔离副本的空测试数据库，严禁用于正式环境。"""
from __future__ import annotations
import sqlite3
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.schema import CaseState, Device, RetrofitProject, ScenarioParams
from app.services import store

@pytest.fixture(autouse=True)
def reset():
    store.reset_all_for_tests()

@pytest.fixture
def c():
    return TestClient(app, raise_server_exceptions=False)

def project(c):
    r=c.post('/api/cases/empty',json={'name':'专业审计企业'})
    assert r.status_code==200
    return r, {'X-Session-Id':r.headers['X-Session-Id'],'X-Project-Token':r.headers['X-Project-Token']}

def upload(c,h,kind,contents):
    return c.post('/api/upload/commit',params={'kind':kind}, headers=h, files={'file':('data.csv',contents.encode('utf-8-sig'),'text/csv')})

def test_public_demo_archive_does_not_expose_private_project_access(c):
    """陌生会话仅浏览公开示范归档，不得取得足以激活别人的私人企业的信息。"""
    r,h=project(c)
    cid=r.json()['case']['case_id']
    a=c.post('/api/devices',headers=h,json={'name':'私有审计资产_仅甲方可见','device_type':'球磨机','power_kw':100,'hours':4000})
    assert a.status_code==200
    rst=c.post('/api/reset-demo',headers=h)
    assert rst.status_code==200
    expo=c.post('/api/reports/export',headers={'X-Session-Id':h['X-Session-Id']},json={'preset':'one_page','format':'txt'})
    assert expo.status_code==200
    other=TestClient(app,raise_server_exceptions=False)
    public=other.get('/api/reports/archives')
    assert public.status_code==200
    rows=public.json().get('reports',[])
    leaked=[row.get('owner_session') for row in rows if row.get('owner_session')==h['X-Session-Id']]
    # 单纯隐藏owner_session还不够：若后端仍允许复用任意X-Session-Id激活私人项目，也未解决根因。
    assert not leaked, '公开示范归档泄露了其他用户的会话凭证'
    replay=other.post(f'/api/cases/{cid}/activate',headers={'X-Session-Id':h['X-Session-Id']})
    assert replay.status_code in (401,403,404), '仅重放会话ID便能激活其他人私有项目'


def test_legacy_duplicate_device_ids_do_not_silently_rebind_retrofit_to_other_equipment():
    """V8.2.4历史数据中重复ID存在，迁移应修复关联或标记待确认，不能选错设备。"""
    old=CaseState(
        case_id='legacy-case',name='旧设备身份修复',
        devices=[
            Device(id='dev-dup',name='磨机A',device_type='球磨机',power_kw=100,hours=4000),
            Device(id='dev-dup',name='磨机B',device_type='球磨机',power_kw=180,hours=4000),
        ],
        retrofits=[RetrofitProject(id='rf-r1',name='磨机B改造',device_id='dev-dup',target_device='磨机B',power_drop_kw=30,investment_cny=10000,annual_maintenance_cny=0)],
        scenario=ScenarioParams(selected_measure_id='rf-r1',hours=4000,energy_price=0.8,emission_factor=0.4471)
    )
    store._dedupe_entity_ids(old)
    m=old.retrofits[0]
    matched=next((d for d in old.devices if d.id==m.device_id),None)
    # 既可绑定正确的磨机B，也可将无法确认的旧措施隔离为待核验，不可仍绑定磨机A。
    assert matched is None or matched.name=='磨机B' or m.id not in {r.id for r in old.retrofits}, (
        '旧ID修复让声明属于磨机B的改造仍指向磨机A，造成静默错绑'
    )


def test_reimport_explicit_new_device_id_must_not_silently_reuse_old_id(c):
    _,h=project(c)
    first=upload(c,h,'device','设备名称,设备类型,功率_kW,年运行小时\n循环泵一号,水泵,100,4000\n')
    assert first.status_code==200
    old_id=c.get('/api/case',headers=h).json()['devices'][0]['id']
    add=c.post('/api/retrofits',headers=h,json={'name':'旧技改','device_id':old_id,'power_drop_kw':10,'investment_cny':10000,'annual_maintenance_cny':0})
    assert add.status_code==200
    updated=upload(c,h,'device','设备名称,设备ID,设备类型,功率_kW,年运行小时\n循环泵一号,dev-new-equipment-external-id,水泵,180,4000\n')
    if updated.status_code != 200:
        assert updated.status_code in (400,409,422), updated.text[:400]
        return
    state=c.get('/api/case',headers=h).json()
    same=state['devices'][0]['id']==old_id
    old_attached=any(m.get('device_id')==old_id for m in state['retrofits'])
    assert not (same and old_attached), (
        '用户显式写了新设备ID，系统却按同名强行复用旧ID并保留旧技改，没有提示或确认'
    )


def test_change_device_type_via_import_invalidates_stale_professional_reference(c):
    _,h=project(c)
    d=c.post('/api/devices',headers=h,json={'name':'循环泵A','device_type':'循环水泵','power_kw':55,'hours':4000,'baseline_kwh':200000})
    assert d.status_code==200
    did=d.json()['case']['devices'][-1]['id']
    r=c.post('/api/retrofits',headers=h,json={
        'name':'水泵改造','device_id':did,'method_code':'pump_hydraulic',
        'model_inputs':{'q_m3_h':100,'head_m':50,'eta_pump_before':0.7,'eta_pump_after':0.8,'eta_motor':0.9,'hours':4000},
        'investment_cny':50000,'annual_maintenance_cny':0,'select':True
    })
    assert r.status_code==200,r.text[:300]
    mid=r.json()['case']['retrofits'][-1]['id']
    r=c.post('/api/references/select',headers=h,json={
        'source_id':'gb-19762-2025','applicable_device_id':did,
        'method_code':'pump_hydraulic','parameter_key':'eta_pump_before',
        'unit':'0-1','choice':'match_trial','apply_intent':'apply_to_trial',
        'parameter_value':0.5,'measure_id':mid
    })
    assert r.status_code==200,r.text[:300]
    r=upload(c,h,'device','设备名称,设备类型,功率_kW,年运行小时,基准年电耗_kWh\n循环泵A,变压器,55,4000,200000\n')
    assert r.status_code==200,r.text[:300]
    case=c.get('/api/case',headers=h).json()
    assert case['devices'][0]['device_type']=='变压器'
    assert c.get('/api/results',headers=h).json()['selected_assessment']['recommendable'] is False
    stale=[sel for sel in case['reference_selections'] if sel['method_code']=='pump_hydraulic' and (sel['active'] or sel['applied'])]
    assert not stale,'设备类型已变压器，水泵专业模型的参考参数仍标记为当前有效'


def test_archive_persistence_failure_cannot_silently_claim_saved(c):
    _,h=project(c)
    with patch.object(store,'_persist_report_archive',side_effect=OSError('模拟磁盘持久化失败')):
        r=c.post('/api/reports/export',headers=h,json={'preset':'one_page','format':'txt'})
    with sqlite3.connect(store.DB_PATH) as db:
        count=db.execute('SELECT count(*) FROM report_archives').fetchone()[0]
    assert count==0
    # 可以允许报告文本临时下载，但响应不能伪称历史归档已成功保存。
    assert r.status_code>=400 or r.headers.get('X-Archive-Saved')=='false' or r.headers.get('X-Archive-Warning'), (
        'SQLite归档失败被吞没，HTTP200没有提示，归档仅存在内存，重启后消失'
    )


def test_demo_core_calculation_stable(c):
    r=c.get('/api/results')
    assert r.status_code==200
    sa=r.json()['selected_assessment']
    assert sa['saved_kwh']==pytest.approx(800000)
    assert sa['co2_reduction_t']==pytest.approx(357.68)


def test_independent_users_with_private_token_are_isolated(c):
    a=TestClient(app,raise_server_exceptions=False)
    b=TestClient(app,raise_server_exceptions=False)
    ra=a.post('/api/cases/empty',json={'name':'审计甲'})
    rb=b.post('/api/cases/empty',json={'name':'审计乙'})
    ha={'X-Session-Id':ra.headers['X-Session-Id'],'X-Project-Token':ra.headers['X-Project-Token']}
    hb={'X-Session-Id':rb.headers['X-Session-Id'],'X-Project-Token':rb.headers['X-Project-Token']}
    r=b.post('/api/cases/'+ra.json()['case']['case_id']+'/activate',headers=hb)
    assert r.status_code in (401,403,404)
    assert a.get('/api/case',headers=ha).json()['case_id']==ra.json()['case']['case_id']
