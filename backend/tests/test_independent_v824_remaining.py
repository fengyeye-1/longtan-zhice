"""Independent V8.2.4 acceptance checks. ONLY IN SEPARATE TEMP CLONE: clears test database!"""
from __future__ import annotations
import json
import sqlite3
import os
import subprocess
import sys
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.services import store
from app.services import import_data

@pytest.fixture(autouse=True)
def isolated_reset():
    store.reset_all_for_tests()

@pytest.fixture
def c():
    return TestClient(app, raise_server_exceptions=False)

def project(c, name='审计企业'):
    r=c.post('/api/cases/empty',json={'name':name})
    assert r.status_code == 200, r.text[:400]
    return r, {'X-Session-Id':r.headers['X-Session-Id'],'X-Project-Token':r.headers['X-Project-Token']}

def csv_upload(c, h, kind, data):
    return c.post(f'/api/upload/commit?kind={kind}',headers=h,
        files={'file':('data.csv',data.encode('utf-8-sig'),'text/csv')})

def persisted_state(case_id):
    with sqlite3.connect(store.DB_PATH) as db:
        row=db.execute('SELECT payload FROM projects WHERE case_id=?',(case_id,)).fetchone()
    return json.loads(row[0]) if row else None

def test_old_version_restore_is_durable_and_new_revision(c):
    r,h=project(c,'版本恢复')
    for price in [0.8, 1.4]:
        u=c.put('/api/scenario',headers=h,json={'energy_price':price})
        assert u.status_code == 200
        if price==0.8:
            ver=c.post('/api/versions/save',headers=h,params={'label':'便宜电价'})
            assert ver.status_code==200
            vid=ver.json()['version_id']
    rr=c.post(f'/api/versions/{vid}/restore',headers=h)
    assert rr.status_code == 200, rr.text[:400]
    restored=c.get('/api/case',headers=h).json()
    durable=persisted_state(restored['case_id'])
    assert durable is not None
    assert restored['scenario']['energy_price']==pytest.approx(0.8)
    assert durable['scenario']['energy_price']==pytest.approx(0.8), 'API返回恢复成功但持久化仍是旧的新版本'
    assert durable['revision']==restored['revision'], '恢复应当是新修订，不应把修订号回退'

def test_successful_write_after_old_restore_is_also_durable(c):
    _,h=project(c)
    c.put('/api/scenario',headers=h,json={'energy_price':0.7})
    vid=c.post('/api/versions/save',headers=h).json()['version_id']
    for p in [0.8,0.9,1.0,1.1]:
        c.put('/api/scenario',headers=h,json={'energy_price':p})
    rr=c.post(f'/api/versions/{vid}/restore',headers=h)
    assert rr.status_code==200
    assert c.put('/api/scenario',headers=h,json={'energy_price':0.75}).status_code==200
    state=c.get('/api/case',headers=h).json()
    db=persisted_state(state['case_id'])
    assert db and db['scenario']['energy_price']==pytest.approx(0.75)
    assert state['revision']==db['revision']

def test_device_import_rejects_or_regenerates_colliding_ids(monkeypatch,c):
    _,h=project(c)
    seq=iter(['abc12300000000000000000000000000','abc12311111111111111111111111111','zzz99900000000000000000000000000'])
    monkeypatch.setattr(import_data.uuid, 'uuid4',lambda: SimpleNamespace(hex=next(seq)))
    r=csv_upload(c,h,'device', '设备名称,设备类型,功率_kW,年运行小时\n设备甲,球磨机,100,1000\n设备乙,球磨机,100,1000\n')
    if r.status_code==200:
        ids=[d['id'] for d in c.get('/api/case',headers=h).json()['devices']]
        assert len(ids)==len(set(ids)), '导入成功的设备ID必须唯一'
    else:
        assert r.status_code in (400,409,422)

def test_retrofit_import_rejects_or_regenerates_colliding_ids(monkeypatch,c):
    _,h=project(c)
    dev=c.post('/api/devices',headers=h,json={'name':'主球磨机','device_type':'球磨机','power_kw':100,'hours':4000})
    assert dev.status_code==200
    seq=iter(['def45600000000000000000000000000','def45611111111111111111111111111','eee33300000000000000000000000000'])
    monkeypatch.setattr(import_data.uuid, 'uuid4',lambda: SimpleNamespace(hex=next(seq)))
    r=csv_upload(c,h,'retrofit', '项目名称,目标设备,功率降低_kW,投资_元,年维护费_元\n方案甲,主球磨机,10,2000,0\n方案乙,主球磨机,5,3000,0\n')
    if r.status_code==200:
        ids=[d['id'] for d in c.get('/api/case',headers=h).json()['retrofits']]
        assert len(ids)==len(set(ids)), '导入成功的技改ID必须唯一'
    else:
        assert r.status_code in (400,409,422)

def test_reimport_same_device_table_does_not_silently_delete_retrofit(c):
    _,h=project(c)
    rows='设备名称,设备类型,功率_kW,年运行小时\n磨机A,球磨机,100,4000\n'
    assert csv_upload(c,h,'device',rows).status_code==200
    did=c.get('/api/case',headers=h).json()['devices'][0]['id']
    add=c.post('/api/retrofits',headers=h,json={'name':'重要技改','device_id':did,'power_drop_kw':10,'investment_cny':10000,'annual_maintenance_cny':0})
    assert add.status_code==200
    before=c.get('/api/case',headers=h).json()
    r=csv_upload(c,h,'device',rows)
    after=c.get('/api/case',headers=h).json()
    if r.status_code==200:
        assert len(after['retrofits'])>=1 or len(after.get('removed_retrofits') or [])>=1, '重新导入同设备后所有技改无回收入口地消失'
    else:
        assert r.status_code in (400,409,422)
        assert after['retrofits']==before['retrofits']

def test_all_owned_projects_remain_listed_after_restart(c):
    a,ha=project(c,'甲企业')
    b=c.post('/api/cases/empty',headers=ha,json={'name':'乙企业'})
    assert b.status_code==200
    sid=ha['X-Session-Id']; tb=b.headers['X-Project-Token']
    ck_sid, ck_mac = c.cookies.get('ltzs_sid'), c.cookies.get('ltzs_stk')
    code = '''from fastapi.testclient import TestClient
from app.main import app
c=TestClient(app)
c.cookies.set("ltzs_sid", %r)
c.cookies.set("ltzs_stk", %r)
h={"X-Session-Id":%r,"X-Project-Token":%r}
r=c.get("/api/cases",headers=h)
print(__import__("json").dumps([x["name"] for x in r.json()["cases"]],ensure_ascii=False))''' %(ck_sid,ck_mac,sid,tb)
    p=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=25,cwd=os.getcwd())
    assert p.returncode==0,p.stderr[:300]
    names=json.loads(p.stdout.strip().splitlines()[-1])
    assert '甲企业' in names and '乙企业' in names, '重启后其他历史项目从列表消失'

def test_retrofit_csv_can_bind_same_named_devices_by_explicit_id(c):
    _,h=project(c)
    for _ in range(2):
        assert c.post('/api/devices',headers=h,json={'name':'冷却泵','device_type':'水泵','power_kw':60,'hours':4000}).status_code==200
    did=c.get('/api/case',headers=h).json()['devices'][1]['id']
    data=f'项目名称,目标设备,设备ID,功率降低_kW,投资_元,年维护费_元\n改造方案,冷却泵,{did},5,10000,0\n'
    r=csv_upload(c,h,'retrofit',data)
    assert r.status_code==200,r.text[:350]
    rf=c.get('/api/case',headers=h).json()['retrofits']
    assert len(rf)==1 and rf[0]['device_id']==did

def test_retrofit_csv_explicit_method_is_respected(c):
    _,h=project(c)
    assert c.post('/api/devices',headers=h,json={'name':'球磨机A','device_type':'球磨机','power_kw':100,'hours':4000}).status_code==200
    data='项目名称,目标设备,功率降低_kW,节电比例,计算方法,投资_元,年维护费_元\n两种参数选功率差,球磨机A,5,0.1,power_drop_hours,10000,0\n'
    r=csv_upload(c,h,'retrofit',data)
    assert r.status_code==200,r.text[:350]
    rf=c.get('/api/case',headers=h).json()['retrofits'][0]
    assert rf['calculation_method']=='power_drop_hours' and rf['power_drop_kw']==5

def test_year_canonicalization_precedes_duplicate_detection(c):
    _,h=project(c)
    r=csv_upload(c,h,'energy','年份,工业用电量_万kWh\n2024,100\n２０２４,200\n')
    if r.status_code==200:
        ys=[y['year'] for y in c.get('/api/case',headers=h).json()['energy_years']]
        assert len(ys)==len(set(ys)), '不同数字字形的同一年被当作不同年份导入'
    else: assert r.status_code in (400,409,422)

def test_unsafe_quote_url_is_rejected_or_sanitized(c):
    _,h=project(c)
    r=c.post('/api/procurement/user-quotes',headers=h,json={
        'label':'供应商检查','device_type':'球磨机','total_quote_cny':10000,
        'source_url':'javascript:alert(824)'})
    if r.status_code==200:
        catalog=c.get('/api/procurement/catalog',headers=h).json()['quotes']
        user=[x for x in catalog if x.get('quote_kind')=='user_quote']
        assert not any(str(x.get('source_url','')).lower().startswith('javascript:') for x in user), '不可把JavaScript伪URL作为可点击报价来源'
    else:
        assert r.status_code in (400,409,422)

def test_safe_quote_url_still_accepted(c):
    _,h=project(c)
    r=c.post('/api/procurement/user-quotes',headers=h,json={
        'label':'合规报价','device_type':'球磨机','total_quote_cny':10000,
        'source_url':'https://example.com/source'})
    assert r.status_code==200

def test_demo_unchanged(c):
    v=c.get('/api/results').json()['selected_assessment']
    assert v['saved_kwh']==pytest.approx(800000)
    assert v['co2_reduction_t']==pytest.approx(357.68)
