"""会话/项目隔离存储：公开示范只读；无全局 ACTIVE 串数据。"""
from __future__ import annotations
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from ..models.schema import (
    CaseState, EnergyYear, Device, RetrofitProject, ScenarioParams, DataGrade,
    DataPrepItem,
)
from ..calc.equipment import COST_INTENSITY_CNY_PER_KWH

ROOT = Path(os.environ.get('LTZS_DATA_DIR') or Path(__file__).resolve().parents[2] / 'data')
VERSIONS = ROOT / 'versions'
DB_PATH = ROOT / 'projects.sqlite'
VERSIONS.mkdir(parents=True, exist_ok=True)
ROOT.mkdir(parents=True, exist_ok=True)
SESSION_SECRET_PATH = ROOT / '.session_hmac'

# 服务端会话票据有效期（秒）。浏览器 Cookie 的 max_age 不能代替这项校验。
SESSION_TTL_SECONDS = int(os.environ.get('LTZS_SESSION_TTL_SECONDS', str(7 * 86400)))
# 空闲匿名会话回收窗口与容量上限（不删除仍持有项目的工作区会话）。
ANON_SESSION_IDLE_SECONDS = int(os.environ.get('LTZS_ANON_SESSION_IDLE_SECONDS', str(30 * 60)))
MAX_ANON_SESSIONS = int(os.environ.get('LTZS_MAX_ANON_SESSIONS', '200'))
_LAST_SESSION_SWEEP_TS = 0
# 权限模型：
# - 已验证的会话 Cookie（ltzs_sid + 未过期且未撤销的 ltzs_stk）= 工作区级身份，可恢复该所有者的全部项目。
# - 项目 Token（X-Project-Token）= 单项目访问凭证，只能读取对应项目，不得提升为工作区凭证。
AUTH_SCOPE_WORKSPACE = 'workspace_session'
AUTH_SCOPE_PROJECT = 'project_token'
AUTH_SCOPE_PUBLIC = 'public_demo'

JINHUI_ID = 'jinhui-demo'

# 进程内：示范模板（只读）+ 会话工作区 + 项目实例
_DEMO: CaseState | None = None
_SESSIONS: dict[str, dict[str, Any]] = {}
_CASES: dict[str, CaseState] = {}  # case_id -> CaseState（非 demo 可写副本）
_TOKENS: dict[str, str] = {}  # token -> case_id
_REPORT_ARCHIVES: dict[str, list[dict[str, Any]]] = {}
_LOCK = threading.RLock()

_ctx_session: ContextVar[Optional[str]] = ContextVar('ltzs_session', default=None)
_ctx_token: ContextVar[Optional[str]] = ContextVar('ltzs_token', default=None)
# 可变容器：Starlette BaseHTTPMiddleware 的 call_next 在子任务中运行，
# ContextVar 本身回写不到中间件，但同一 dict 引用可以带回切换后的 Token。
_ctx_auth: ContextVar[Optional[dict[str, Any]]] = ContextVar('ltzs_auth', default=None)


class VersionConflict(Exception):
    """同一项目的过期草稿提交：必须可解释拒绝，不得静默覆盖。"""
    status_code = 409

    def __init__(self, message: str = '版本已变化，请刷新后重试'):
        super().__init__(message)
        self.status_code = 409


def build_data_prep(case: CaseState) -> list[DataPrepItem]:
    """准备项跟随当前选中措施，避免选泵仍强调球磨功率。"""
    sc = case.scenario
    items: list[DataPrepItem] = []
    measure = None
    if sc.selected_measure_id:
        measure = next((r for r in case.retrofits if r.id == sc.selected_measure_id), None)
    use_pump = bool(
        measure is not None
        and measure.saving_fraction is not None
        and measure.power_drop_kw is None
    )

    if use_pump:
        pump_base = sc.pump_baseline_kwh
        if pump_base is None and measure and measure.device_id:
            d = next((x for x in case.devices if x.id == measure.device_id), None)
            if d and d.baseline_kwh is not None:
                pump_base = d.baseline_kwh
        if pump_base is None:
            pump_dev = next((d for d in case.devices if d.device_type == '泵' or '泵' in d.name), None)
            if pump_dev and pump_dev.baseline_kwh is not None:
                pump_base = pump_dev.baseline_kwh
        if pump_base is None:
            items.append(DataPrepItem(
                key='pump_baseline',
                label='水泵改造前一年用电量',
                status='needed',
                detail='选中水泵措施后需要填写，才能计算节电',
                module='pump',
            ))
        else:
            items.append(DataPrepItem(
                key='pump_baseline',
                label='水泵改造前一年用电量',
                status='ready' if case.is_demo else 'assumed',
                detail=f'{pump_base:g} kWh',
                module='pump',
            ))
        items.append(DataPrepItem(
            key='pump_rate',
            label='水泵节电比例',
            status='ready' if (measure and measure.saving_fraction is not None) else 'needed',
            detail=(
                f'{measure.saving_fraction * 100:.2f}%'
                if measure and measure.saving_fraction is not None else '待填写'
            ),
            module='pump',
        ))
    else:
        pd = measure.power_drop_kw if measure and measure.power_drop_kw is not None else sc.power_drop_kw
        if pd is not None:
            items.append(DataPrepItem(
                key='power_drop',
                label=f'功率降低 {pd:g} kW',
                status='ready' if case.is_demo else 'assumed',
                detail='公开披露（历史技改复盘）' if case.is_demo else '当前项目参数',
                module='mill',
            ))
        else:
            items.append(DataPrepItem(
                key='power_drop',
                label='功率降低',
                status='needed',
                detail='请选择技改措施或填写功率降幅',
                module='mill',
            ))
        if sc.hours is not None:
            items.append(DataPrepItem(
                key='hours',
                label='有效节能小时',
                status='assumed',
                detail=f'当前采用假设 {sc.hours:g} h',
                module='mill',
            ))
        else:
            items.append(DataPrepItem(
                key='hours',
                label='有效节能小时',
                status='needed',
                detail='请填写年有效节能小时',
                module='mill',
            ))

    # 投资：看选中措施自身，不把全局情景投资误认为泵已填
    inv = None
    if measure is not None and measure.investment_cny is not None:
        inv = measure.investment_cny
    elif getattr(sc, 'investment_bound_measure_id', None) and measure and sc.investment_bound_measure_id == measure.id:
        inv = sc.investment_cny
    elif measure is None:
        inv = sc.investment_cny
    if inv is None:
        items.append(DataPrepItem(
            key='investment',
            label='改造投资',
            status='needed',
            detail='未填时仍可算节电/减排；回收期需补充本措施投资',
            module='roi',
        ))
    else:
        items.append(DataPrepItem(
            key='investment',
            label='改造投资',
            status='assumed',
            detail=f'{inv:,.0f} 元（绑定当前措施）',
            module='roi',
        ))

    items.append(DataPrepItem(
        key='hourly',
        label='逐小时负荷曲线',
        status='optional_advanced',
        detail='年度节电分析不需要；已移入高级功能',
        module='advanced',
    ))
    return items



def _jinhui_case() -> CaseState:
    energy = [
        EnergyYear(year=2023, elec_10k_kwh=9706.76, cost_10k_cny=4437.43,
                   diesel_10k_l=263.48, diesel_cost_10k_cny=1703.63, note='公司合并披露',
                   url='https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=9891039&stockid=603132'),
        EnergyYear(year=2024, elec_10k_kwh=11777.17, cost_10k_cny=5553.47,
                   diesel_10k_l=297.51, diesel_cost_10k_cny=1848.85, note='公司合并披露',
                   url='https://big5.sse.com.cn/site/cht/www.sse.com.cn/disclosure/listedinfo/announcement/c/new/2025-03-11/603132_20250311_V9UM.pdf'),
        EnergyYear(year=2025, elec_10k_kwh=13612.92, cost_10k_cny=6191.2,
                   diesel_10k_l=430.72, diesel_cost_10k_cny=2476.18,
                   note='公司合并披露；多个采矿与建设项目',
                   url='https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12020873&stockid=603132'),
    ]
    devices = [
        Device(id='dev-mill', name='选厂球磨机', device_type='球磨',
               power_kw=None, hours=None, data_type='披露',
               source='2024年报仅披露功率下降200kW，未披露基准功率与全年小时'),
        Device(id='dev-pump', name='二级输送泵', device_type='泵',
               power_kw=None, hours=None, baseline_kwh=None, data_type='披露',
               source='2024年报披露电耗下降12.93%，需设备基准kWh'),
    ]
    retrofits = [
        RetrofitProject(
            id='rf-mill', name='碎磨工艺改进', target_device='选厂球磨机',
            device_id='dev-mill',
            power_drop_kw=200, investment_cny=1_000_000,
            annual_maintenance_cny=0, data_type='披露',
            source='金徽2024年报', note='未披露全年有效节能小时；运维按假设无新增'),
        RetrofitProject(
            id='rf-pump', name='二级输送泵改进', target_device='二级输送泵',
            device_id='dev-pump',
            saving_fraction=0.1293, annual_maintenance_cny=0, data_type='披露',
            source='金徽2024年报', note='不能乘公司总用电'),
    ]
    sources = [
        {'name': '2023年报', 'category': '企业公开披露', 'url': energy[0].url},
        {'name': '2024年报', 'category': '企业公开披露', 'url': energy[1].url},
        {'name': '2025年报', 'category': '企业公开披露', 'url': energy[2].url},
        {'name': '甘肃2023电力因子', 'category': '官方因子',
         'url': 'https://www.mee.gov.cn/xxgk2018/xxgk/xxgk01/202512/W020251231726284332528.pdf'},
        {'name': 'MARS磨机论文', 'category': '同行业论文',
         'url': 'https://www.mdpi.com/2075-163X/16/11/1109'},
    ]
    case = CaseState(
        case_id=JINHUI_ID,
        name='金徽公开案例—球磨机历史案例复盘',
        is_demo=True,
        protected=True,
        data_grade='annual',
        energy_years=energy,
        devices=devices,
        retrofits=retrofits,
        scenario=ScenarioParams(
            power_drop_kw=200,
            hours=4000,
            baseline_power_kw=None,
            pump_baseline_kwh=None,
            energy_price=COST_INTENSITY_CNY_PER_KWH,
            emission_factor=0.4471,
            investment_cny=1_000_000,
            annual_maintenance_cny=0,
            assume_zero_maintenance=True,
            maintenance_bound_measure_id='rf-mill',
            use_demo_baseline=False,
            use_jinhui_reference=True,
            use_jinhui_tariff_reference=False,
            selected_measure_id='rf-mill',
            emission_factor_user_override=False,
            energy_price_user_override=False,
        ),
        missing_fields=[],
        sources=sources,
        version_label='demo-protected',
        access_token=None,
        owner_session=None,
    )
    case.data_prep = build_data_prep(case)
    return case


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS projects (
            case_id TEXT PRIMARY KEY,
            token TEXT UNIQUE NOT NULL,
            owner_session TEXT,
            payload TEXT NOT NULL,
            created_at TEXT,
            updated_at TEXT
        )'''
    )
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS versions (
            version_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            token TEXT NOT NULL,
            owner_session TEXT,
            label TEXT,
            payload TEXT NOT NULL,
            saved_at TEXT
        )'''
    )
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS session_tickets (
            ticket_id TEXT PRIMARY KEY,
            sid TEXT NOT NULL,
            issued_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0
        )'''
    )
    conn.execute('CREATE INDEX IF NOT EXISTS idx_session_tickets_sid ON session_tickets(sid)')
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS report_archives (
            export_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            owner_session TEXT,
            snapshot_id TEXT,
            payload TEXT NOT NULL,
            created_at TEXT
        )'''
    )
    conn.commit()
    return conn


def _payload_revision(payload: str) -> int:
    try:
        data = json.loads(payload) if payload else {}
        return int((data or {}).get('revision') or 0)
    except Exception:
        return 0


def _dedupe_entity_ids(state: CaseState) -> list[str]:
    """修复历史重复设备/技改 ID，并按名称重绑关联；无法唯一判定时标记待核查。"""
    notes: list[str] = []

    def _alloc(prefix: str, taken: set[str]) -> str:
        while True:
            nid = f'{prefix}-{uuid.uuid4().hex}'
            if nid not in taken:
                taken.add(nid)
                return nid

    def fix_simple(items, prefix: str, label: str, taken: set[str]) -> None:
        seen: set[str] = set()
        for item in items or []:
            cid = getattr(item, 'id', None)
            if not cid:
                item.id = _alloc(prefix, taken)
                notes.append(f'{label}缺少编号，已分配 {item.id}')
                seen.add(item.id)
                continue
            if cid in seen:
                old = cid
                item.id = _alloc(prefix, taken)
                notes.append(f'发现重复{label}ID {old}，已重新分配为 {item.id}')
            else:
                seen.add(cid)
                taken.add(cid)

    devices = list(getattr(state, 'devices', None) or [])
    removed_devices = list(getattr(state, 'removed_devices', None) or [])
    all_dev_items = devices + removed_devices
    groups: dict[str, list[Any]] = {}
    for d in all_dev_items:
        did = getattr(d, 'id', None)
        if did:
            groups.setdefault(did, []).append(d)

    taken_dev: set[str] = set()
    shared_members: dict[str, list[tuple[str, str, Any]]] = {}
    for old_id, members in groups.items():
        if len(members) <= 1:
            if members:
                taken_dev.add(old_id)
                shared_members[old_id] = [(old_id, getattr(members[0], 'name', '') or '', members[0])]
            continue
        entry: list[tuple[str, str, Any]] = []
        first = members[0]
        taken_dev.add(old_id)
        entry.append((old_id, getattr(first, 'name', '') or '', first))
        for dup in members[1:]:
            new_id = _alloc('dev', taken_dev)
            notes.append(f'发现重复设备ID {old_id}，已重新分配为 {new_id}')
            dup.id = new_id
            entry.append((new_id, getattr(dup, 'name', '') or '', dup))
        shared_members[old_id] = entry

    for d in all_dev_items:
        if not getattr(d, 'id', None):
            d.id = _alloc('dev', taken_dev)
            notes.append(f'设备缺少编号，已分配 {d.id}')

    ambiguous_old_ids = {oid for oid, members in groups.items() if len(members) > 1}

    def _rebind_device_ref(current_id: str | None, hint_name: str | None = None) -> tuple[str | None, str]:
        if not current_id or current_id not in ambiguous_old_ids:
            return current_id, 'unchanged'
        members = shared_members.get(current_id) or []
        hint = (hint_name or '').strip()
        if hint:
            exact = [(fid, name) for fid, name, _ in members if name == hint]
            if len(exact) == 1:
                return exact[0][0], 'rebound'
            soft = [(fid, name) for fid, name, _ in members if hint in name or name in hint]
            if len(soft) == 1:
                return soft[0][0], 'rebound'
        if len(members) == 1:
            return members[0][0], 'rebound'
        return None, 'unbound'

    def _mark_pending(rf: Any, reason: str) -> None:
        extra = f'｜待核查：{reason}'
        note = getattr(rf, 'note', None) or ''
        if '待核查' not in note:
            rf.note = (note + extra).strip('｜')
        if hasattr(rf, 'lifecycle_status'):
            rf.lifecycle_status = 'invalid'
        sc = getattr(state, 'scenario', None)
        if sc is not None and getattr(sc, 'selected_measure_id', None) == getattr(rf, 'id', None):
            sc.selected_measure_id = None

    for rf in list(getattr(state, 'retrofits', None) or []) + list(getattr(state, 'removed_retrofits', None) or []):
        old = getattr(rf, 'device_id', None)
        if not old or old not in ambiguous_old_ids:
            continue
        hint = getattr(rf, 'target_device', None) or ''
        new_id, status = _rebind_device_ref(old, hint)
        if status == 'rebound' and new_id:
            if new_id != old:
                notes.append(f'技改「{getattr(rf, "name", "")}」已按设备名称重绑至 {new_id}')
            rf.device_id = new_id
        else:
            rf.device_id = None
            _mark_pending(rf, '历史重复设备ID无法唯一绑定，已解除并禁止推荐')
            notes.append(f'技改「{getattr(rf, "name", "")}」因重复设备ID无法唯一绑定，已标记待核查')

    for sel in list(getattr(state, 'reference_selections', None) or []):
        did = getattr(sel, 'applicable_device_id', None)
        if not did or did not in ambiguous_old_ids:
            continue
        hint = None
        mid = getattr(sel, 'measure_id', None)
        if mid:
            m = next((r for r in (state.retrofits or []) if r.id == mid), None)
            if m is None:
                m = next((r for r in (getattr(state, 'removed_retrofits', None) or []) if r.id == mid), None)
            if m is not None:
                hint = getattr(m, 'target_device', None)
        new_id, status = _rebind_device_ref(did, hint)
        if status == 'rebound' and new_id:
            sel.applicable_device_id = new_id
        else:
            if hasattr(sel, 'active'):
                sel.active = False
            if hasattr(sel, 'applied'):
                sel.applied = False
            if hasattr(sel, 'superseded'):
                sel.superseded = True
            if hasattr(sel, 'invalidated_reason'):
                sel.invalidated_reason = sel.invalidated_reason or '历史重复设备ID无法唯一绑定，已标记待核查'
            notes.append('参考绑定因重复设备ID无法唯一归属，已失效待核查')

    qc = getattr(state, 'quote_choice', None)
    if qc is not None:
        qdid = getattr(qc, 'device_id', None)
        if qdid and qdid in ambiguous_old_ids:
            new_id, status = _rebind_device_ref(qdid, None)
            if status == 'rebound' and new_id:
                qc.device_id = new_id
            else:
                qc.device_id = None

    for uq in list(getattr(state, 'user_quotes', None) or []):
        if not isinstance(uq, dict):
            continue
        qdid = uq.get('device_id')
        if qdid and qdid in ambiguous_old_ids:
            new_id, status = _rebind_device_ref(qdid, uq.get('device_name') or uq.get('name'))
            if status == 'rebound' and new_id:
                uq['device_id'] = new_id
            else:
                uq['device_id'] = None
                uq['pending_verification'] = True

    sc = getattr(state, 'scenario', None)
    if sc is not None:
        cur = getattr(sc, 'pump_baseline_bound_device_id', None)
        if cur and cur in ambiguous_old_ids:
            new_id, status = _rebind_device_ref(cur, None)
            if status == 'rebound' and new_id:
                sc.pump_baseline_bound_device_id = new_id
            else:
                sc.pump_baseline_bound_device_id = None

    taken_rf: set[str] = set()
    fix_simple(getattr(state, 'retrofits', None), 'rf', '技改', taken_rf)
    fix_simple(getattr(state, 'removed_retrofits', None), 'rf', '回收技改', taken_rf)
    return notes


def _rehydrate_session_projects(sess: dict[str, Any], owner_sid: str | None = None) -> None:
    """仅在「已验证的工作区会话」下，按 owner_session 恢复该所有者的全部项目。

    不得用于项目 Token 登录。owner_sid 必须是当前已通过 Cookie 校验的会话身份
    （或其等价工作区凭证），不得用调用方声称的 SID 或单个项目 Token 扩大授权范围。
    """
    sid = owner_sid or sess.get('session_id')
    if not sid:
        return
    owned = sess.setdefault('owned_case_ids', set())
    for cid, case in list(_CASES.items()):
        if getattr(case, 'owner_session', None) == sid:
            owned.add(cid)
    try:
        conn = _db()
        rows = conn.execute('SELECT case_id, token, payload FROM projects').fetchall()
        conn.close()
    except Exception:
        return
    for case_id, token, payload in rows:
        try:
            data = json.loads(payload)
        except Exception:
            continue
        if data.get('owner_session') != sid:
            continue
        owned.add(case_id)
        if case_id in _CASES:
            continue
        try:
            state = CaseState.model_validate(data)
            state.access_token = token or state.access_token
            notes = _dedupe_entity_ids(state)
            _CASES[case_id] = state
            if state.access_token:
                _TOKENS[state.access_token] = case_id
            if notes:
                state.revision = int(getattr(state, 'revision', 0) or 0) + 1
                _persist_case(state)
        except Exception:
            continue


def _persist_case(case: CaseState) -> None:
    """在调用方已持有的锁内或本函数锁内落盘；禁止用更旧 revision 覆盖更新记录。"""
    if case.protected or case.case_id == JINHUI_ID:
        return
    if not case.access_token:
        return
    now = datetime.now().isoformat(timespec='seconds')
    incoming = int(getattr(case, 'revision', 0) or 0)
    payload = json.dumps(case.model_dump(), ensure_ascii=False)
    with _LOCK:
        conn = _db()
        row = conn.execute(
            'SELECT payload FROM projects WHERE case_id=?', (case.case_id,)
        ).fetchone()
        if row and _payload_revision(row[0]) > incoming:
            conn.close()
            return
        conn.execute(
            '''INSERT INTO projects(case_id, token, owner_session, payload, created_at, updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(case_id) DO UPDATE SET
                 payload=excluded.payload,
                 updated_at=excluded.updated_at,
                 token=excluded.token,
                 owner_session=excluded.owner_session''',
            (case.case_id, case.access_token, case.owner_session, payload, now, now),
        )
        conn.commit()
        conn.close()


def _load_persisted() -> None:
    with _LOCK:
        conn = _db()
        rows = conn.execute('SELECT case_id, token, payload FROM projects').fetchall()
        conn.close()
        for case_id, token, payload in rows:
            try:
                state = CaseState.model_validate(json.loads(payload))
                state.access_token = token
                notes = _dedupe_entity_ids(state)
                _CASES[case_id] = state
                _TOKENS[token] = case_id
                if notes:
                    state.revision = int(getattr(state, 'revision', 0) or 0) + 1
                    _persist_case(state)
            except Exception:
                continue


def init_store() -> None:
    """重置示范与内存会话；保留已持久化项目（测试可清库）。"""
    global _DEMO
    with _LOCK:
        _DEMO = _jinhui_case()
        _SESSIONS.clear()
        # 不清 _CASES/_TOKENS 以便重启恢复；测试用 reset_all
        if not _CASES:
            _load_persisted()




def _persist_report_archive(rec: dict[str, Any]) -> None:
    now = rec.get('exported_at') or datetime.now().isoformat(timespec='seconds')
    payload = json.dumps(rec, ensure_ascii=False, default=str)
    with _LOCK:
        conn = _db()
        conn.execute(
            """INSERT INTO report_archives(export_id, case_id, owner_session, snapshot_id, payload, created_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(export_id) DO UPDATE SET
                 payload=excluded.payload,
                 case_id=excluded.case_id,
                 owner_session=excluded.owner_session,
                 snapshot_id=excluded.snapshot_id""",
            (
                rec.get('export_id'),
                rec.get('case_id') or '',
                rec.get('owner_session'),
                rec.get('snapshot_id'),
                payload,
                now,
            ),
        )
        conn.commit()
        conn.close()


def archive_exported_report(record: dict[str, Any]) -> dict[str, Any]:
    """冻结一份已导出报告，事后参数撤销不得改写这份快照；写入 SQLite 以便重启后按项目读取。"""
    sid = current_session_id()
    rec = dict(record)
    rec.setdefault('export_id', f"exp-{uuid.uuid4().hex[:12]}")
    rec.setdefault('case_id', rec.get('case_id') or get_active_id())
    rec.setdefault('owner_session', sid)
    rec['id_note'] = 'export_id 是归档唯一编号；snapshot_id 仅标识计算情景指纹'
    # 先落盘再入内存：磁盘失败时不得假装已归档
    _persist_report_archive(rec)
    with _LOCK:
        _REPORT_ARCHIVES.setdefault(sid, []).append(rec)
    return rec


def _public_archive_view(rec: dict[str, Any]) -> dict[str, Any]:
    """对外返回的归档视图：剥离会话/令牌等身份字段，防止公开示范归档成为冒用入口。"""
    if not isinstance(rec, dict):
        return {}
    safe = dict(rec)
    for k in ('owner_session', 'access_token', 'project_token', 'token', 'session_id'):
        safe.pop(k, None)
    return safe


def list_exported_reports() -> list[dict[str, Any]]:
    """按当前已授权项目返回归档。优先读 SQLite，保证重启后仍在。"""
    try:
        cid = get_active_id()
    except Exception:
        cid = None
    sid = current_session_id()
    sess = _SESSIONS.get(sid) or {}
    owned = set(sess.get('owned_case_ids') or [])
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    with _LOCK:
        if cid:
            conn = _db()
            if cid == JINHUI_ID:
                rows = conn.execute(
                    'SELECT payload, owner_session FROM report_archives WHERE case_id=? ORDER BY created_at',
                    (cid,),
                ).fetchall()
            else:
                # 已授权项目按 case_id 取归档。Token 登录在重启后会话号会更新，
                # 不得再要求 owner_session 等于当前 SID，否则历史会悄然消失。
                if cid in owned or _session_can_access(cid):
                    rows = conn.execute(
                        'SELECT payload, owner_session FROM report_archives WHERE case_id=? ORDER BY created_at',
                        (cid,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT payload, owner_session FROM report_archives "
                        "WHERE case_id=? AND (owner_session=? OR owner_session IS NULL) "
                        "ORDER BY created_at",
                        (cid, sid),
                    ).fetchall()
            conn.close()
            for payload, owner_session in rows:
                try:
                    rec = json.loads(payload)
                except Exception:
                    continue
                if cid == JINHUI_ID:
                    out.append(_public_archive_view(rec))
                else:
                    if owner_session and owner_session != sid and cid not in owned:
                        # 仍允许当前 Token 对应项目的历史
                        if not _session_can_access(cid):
                            continue
                    out.append(_public_archive_view(rec))
                eid = rec.get('export_id')
                if eid:
                    seen.add(eid)
        for rec in list(_REPORT_ARCHIVES.get(sid) or []):
            if cid and rec.get('case_id') and rec.get('case_id') != cid:
                continue
            eid = rec.get('export_id')
            if eid and eid in seen:
                continue
            if eid:
                seen.add(eid)
            out.append(_public_archive_view(rec))
    return out


def get_exported_report(export_id: str) -> dict[str, Any] | None:
    recs = list_exported_reports()
    for rec in recs:
        if rec.get('export_id') == export_id:
            return rec
    matches = [rec for rec in recs if rec.get('snapshot_id') == export_id]
    if len(matches) == 1:
        return matches[0]
    return matches[-1] if matches else None


def reset_all_for_tests() -> None:
    """测试专用：清空全部会话与项目。"""
    global _DEMO
    with _LOCK:
        _DEMO = _jinhui_case()
        _SESSIONS.clear()
        _CASES.clear()
        _TOKENS.clear()
        _REPORT_ARCHIVES.clear()
        if DB_PATH.exists():
            DB_PATH.unlink()
        # 清版本目录中的 json（兼容旧）
        for p in VERSIONS.glob('*.json'):
            try:
                p.unlink()
            except Exception:
                pass
        conn = _db()
        conn.close()


_SESSION_SECRET: bytes | None = None


def _session_secret() -> bytes:
    """进程级 HMAC 密钥；落在本地 data/.session_hmac，随机器重启仍有效，不进入交付 ZIP。"""
    global _SESSION_SECRET
    if _SESSION_SECRET:
        return _SESSION_SECRET
    try:
        if SESSION_SECRET_PATH.exists():
            raw = SESSION_SECRET_PATH.read_bytes().strip()
            if raw:
                _SESSION_SECRET = raw
                return _SESSION_SECRET
    except Exception:
        pass
    _SESSION_SECRET = secrets.token_bytes(32)
    try:
        SESSION_SECRET_PATH.write_bytes(_SESSION_SECRET)
        try:
            SESSION_SECRET_PATH.chmod(0o600)
        except Exception:
            pass
    except Exception:
        pass
    return _SESSION_SECRET


def _now_ts() -> int:
    """当前 Unix 时间戳；测试可替换本函数做时间旅行。"""
    return int(time.time())


def session_ttl_seconds() -> int:
    return int(SESSION_TTL_SECONDS)


def _blank_session(sid: str) -> dict[str, Any]:
    now = _now_ts()
    return {
        "session_id": sid,
        "active_case_id": JINHUI_ID,
        "project_token": None,
        "owned_case_ids": set(),
        "created_at": now,
        "last_seen": now,
    }


def _touch_session(sess: dict[str, Any] | None) -> None:
    if sess is not None:
        sess["last_seen"] = _now_ts()


def _is_anonymous_session(sess: dict[str, Any] | None) -> bool:
    if not sess:
        return False
    owned = sess.get("owned_case_ids") or set()
    if owned:
        return False
    if sess.get("project_token"):
        return False
    active = sess.get("active_case_id")
    if active not in (None, "", JINHUI_ID):
        return False
    return True


def sweep_expired_session_state() -> dict[str, int]:
    """清理过期/已撤销票据与空闲匿名内存会话。不删除项目、版本或有效工作区会话。"""
    global _LAST_SESSION_SWEEP_TS
    now = _now_ts()
    deleted_tickets = 0
    evicted_sessions = 0
    with _LOCK:
        conn = _db()
        cur = conn.execute(
            "DELETE FROM session_tickets WHERE expires_at < ? OR (revoked=1 AND expires_at < ?)",
            (now, now),
        )
        deleted_tickets = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
        conn.commit()
        conn.close()
        idle = int(ANON_SESSION_IDLE_SECONDS)
        to_drop: list[str] = []
        for sid, sess in list(_SESSIONS.items()):
            if not _is_anonymous_session(sess):
                continue
            last = int(sess.get("last_seen") or sess.get("created_at") or 0)
            if last and now - last > idle:
                to_drop.append(sid)
        for sid in to_drop:
            _SESSIONS.pop(sid, None)
            evicted_sessions += 1
        cap = max(1, int(MAX_ANON_SESSIONS))
        anons = [
            (sid, int((_SESSIONS[sid].get("last_seen") or _SESSIONS[sid].get("created_at") or 0)))
            for sid in list(_SESSIONS)
            if _is_anonymous_session(_SESSIONS[sid])
        ]
        if len(anons) > cap:
            anons.sort(key=lambda item: item[1])
            for sid, _ts in anons[: len(anons) - cap]:
                _SESSIONS.pop(sid, None)
                evicted_sessions += 1
        _LAST_SESSION_SWEEP_TS = now
    return {"deleted_tickets": deleted_tickets, "evicted_sessions": evicted_sessions}


def maybe_sweep_session_state(force: bool = False, min_interval: int = 30) -> dict[str, int] | None:
    """默认最多每 30 秒清扫一次；测试可 force=True。"""
    global _LAST_SESSION_SWEEP_TS
    now = _now_ts()
    if not force and (now - int(_LAST_SESSION_SWEEP_TS or 0)) < max(0, int(min_interval)):
        return None
    return sweep_expired_session_state()


def _sign_session_ticket(sid: str, issued_at: int) -> str:
    payload = f"{sid}:{int(issued_at)}".encode("utf-8")
    return hmac.new(_session_secret(), payload, hashlib.sha256).hexdigest()


def _format_session_mac(issued_at: int, digest: str) -> str:
    return f"{int(issued_at)}.{digest}"


def _parse_session_mac(mac: str) -> tuple[int | None, str]:
    raw = str(mac or "").strip()
    if "." in raw:
        ts_s, digest = raw.split(".", 1)
        try:
            return int(ts_s), digest.strip()
        except ValueError:
            return None, raw
    return None, raw


def _persist_session_ticket(ticket_id: str, sid: str, issued_at: int, expires_at: int) -> None:
    sql = (
        "INSERT INTO session_tickets(ticket_id, sid, issued_at, expires_at, revoked) "
        "VALUES(?,?,?,?,0) "
        "ON CONFLICT(ticket_id) DO UPDATE SET expires_at=excluded.expires_at"
    )
    with _LOCK:
        conn = _db()
        conn.execute(sql, (ticket_id, sid, int(issued_at), int(expires_at)))
        conn.commit()
        conn.close()


def _ticket_row(sid: str, issued_at: int | None = None) -> tuple | None:
    with _LOCK:
        conn = _db()
        if issued_at is None:
            row = conn.execute(
                "SELECT ticket_id, sid, issued_at, expires_at, revoked "
                "FROM session_tickets WHERE sid=? AND revoked=0 "
                "ORDER BY issued_at DESC LIMIT 1",
                (sid,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT ticket_id, sid, issued_at, expires_at, revoked "
                "FROM session_tickets WHERE sid=? AND issued_at=? LIMIT 1",
                (sid, int(issued_at)),
            ).fetchone()
        conn.close()
        return row


def _revoke_ticket_rows(sid: str, issued_at: int | None = None) -> None:
    with _LOCK:
        conn = _db()
        if issued_at is None:
            conn.execute("UPDATE session_tickets SET revoked=1 WHERE sid=? AND revoked=0", (sid,))
        else:
            conn.execute(
                "UPDATE session_tickets SET revoked=1 WHERE sid=? AND issued_at=?",
                (sid, int(issued_at)),
            )
        conn.commit()
        conn.close()


def issue_session_mac(sid: str, issued_at: int | None = None, *, rotate: bool = False) -> str:
    """为会话号签发带时间戳的 HMAC，写入独立 Cookie ltzs_stk。

    Cookie 值格式：`<issued_at>.<hmac(sid:issued_at)>`。服务端按签发时间过期，
    并在注销时撤销；浏览器 Cookie 的 7 天 max_age 不能单独作为有效期证明。
    默认复用该 SID 上未过期、未撤销的票据，避免每次响应都轮换。
    """
    if not sid:
        return ""
    now = _now_ts()
    ttl = session_ttl_seconds()
    if not rotate and issued_at is None:
        row = _ticket_row(sid)
        if row and int(row[4] or 0) == 0 and int(row[3]) > now:
            issued_at = int(row[2])
            digest = _sign_session_ticket(sid, issued_at)
            return _format_session_mac(issued_at, digest)
    if issued_at is None:
        issued_at = now
    expires_at = int(issued_at) + ttl
    digest = _sign_session_ticket(sid, int(issued_at))
    ticket_id = hashlib.sha256(f"{sid}:{int(issued_at)}".encode("utf-8")).hexdigest()[:32]
    try:
        _persist_session_ticket(ticket_id, sid, int(issued_at), expires_at)
    except Exception:
        # 落盘失败仍签发，但服务端重启后只能靠 HMAC+时间窗恢复，无法做主动撤销。
        pass
    return _format_session_mac(int(issued_at), digest)


def verify_session_mac(sid: str | None, mac: str | None) -> bool:
    """校验 Cookie 签名、服务端过期与撤销状态。

    自 8.2.9 起不再接受 8.2.7 无签发时间的旧格式 HMAC。旧格式无法做服务端
    撤销与过期检查，若继续兼容，注销后仍可能凭历史签名恢复工作区。
    """
    if not sid or not mac:
        return False
    issued_at, digest = _parse_session_mac(str(mac))
    now = _now_ts()
    ttl = session_ttl_seconds()
    if issued_at is None:
        return False
    expected = _sign_session_ticket(sid, issued_at)
    try:
        if not hmac.compare_digest(expected, digest):
            return False
    except Exception:
        return False
    if issued_at + ttl < now:
        return False
    row = _ticket_row(sid, issued_at)
    if row and int(row[4] or 0) == 1:
        return False
    if row and int(row[3]) < now:
        return False
    if row is None:
        try:
            ticket_id = hashlib.sha256(f"{sid}:{int(issued_at)}".encode("utf-8")).hexdigest()[:32]
            _persist_session_ticket(ticket_id, sid, int(issued_at), int(issued_at) + ttl)
        except Exception:
            pass
    return True


def maybe_rotate_session_mac(sid: str, mac: str | None) -> str:
    """滑动刷新：已过半有效期则签发新票据并撤销旧票据。"""
    if not sid:
        return ""
    issued_at, _digest = _parse_session_mac(str(mac or ""))
    now = _now_ts()
    ttl = session_ttl_seconds()
    if issued_at is not None and (now - issued_at) < (ttl / 2):
        return issue_session_mac(sid, issued_at=issued_at)
    new_mac = issue_session_mac(sid, rotate=True)
    if issued_at is not None:
        try:
            _revoke_ticket_rows(sid, issued_at)
        except Exception:
            pass
    return new_mac


def revoke_session(sid: str | None = None) -> str:
    """主动注销当前（或指定）会话的全部未撤销票据，并开启新的匿名会话。"""
    target = sid or current_session_id()
    if target:
        try:
            _revoke_ticket_rows(target)
        except Exception:
            pass
        with _LOCK:
            _SESSIONS.pop(target, None)
    new_sid = secrets.token_urlsafe(16)
    with _LOCK:
        _SESSIONS[new_sid] = _blank_session(new_sid)
    _ctx_session.set(new_sid)
    _ctx_token.set(None)
    _ctx_auth.set({"token": None, "rebound": False, "scope": AUTH_SCOPE_PUBLIC})
    bind_request_token(None)
    return new_sid


def current_auth_scope() -> str:
    info = _ctx_auth.get() or {}
    return str(info.get("scope") or AUTH_SCOPE_PUBLIC)


def set_request_context(
    session_id: str | None,
    token: str | None = None,
    *,
    trusted: bool | None = None,
    cookie_verified: bool | None = None,
) -> str:
    """绑定当前请求的会话；返回 session_id。

    权限关系（必须同时在文档中保持一致）：
    - 工作区级身份：仅来自已校验且未过期、未撤销的 Cookie（ltzs_sid + ltzs_stk）。
      此时可按 owner_session 恢复该所有者名下的全部项目。
    - 项目 Token：只授权 Token 对应的那一个案例；不能列出、激活或导出兄弟项目，
      也不得把单个项目 Token 提升为工作区凭证。
    - 未校验的 X-Session-Id（含进程重启后内存为空、仅重放历史 SID）不得占用已有
      历史身份，也不得被签发新的认证签名。
    - 进程内旧调用若显式 trusted=True，或同时给了 token 且未传 cookie_verified，
      保持原兼容行为（仅限非 HTTP 中间件直调）。
    """
    claimed = session_id or None
    if cookie_verified is True:
        identity_trusted = True
    elif cookie_verified is False:
        identity_trusted = False
    elif trusted is not None:
        identity_trusted = bool(trusted)
    else:
        # 兼容直接调用：历史测试以 token 授权该项目
        identity_trusted = bool(token)

    token_cid = _case_id_for_token(token) if token else None

    try:
        maybe_sweep_session_state()
    except Exception:
        pass

    with _LOCK:
        if cookie_verified is False and token_cid:
            # HTTP 层：无有效会话票据时，忽略声称的 SID，避免 A的SID+B的Token 展开 A 的项目
            sid = secrets.token_urlsafe(16)
            _SESSIONS[sid] = _blank_session(sid)
        elif identity_trusted and claimed:
            sid = claimed
            if sid not in _SESSIONS:
                _SESSIONS[sid] = _blank_session(sid)
            else:
                _touch_session(_SESSIONS[sid])
        else:
            # 未校验身份：一律签发全新会话，绝不沿用请求头里的历史 SID。
            sid = secrets.token_urlsafe(16)
            _SESSIONS[sid] = _blank_session(sid)

    _ctx_session.set(sid)
    _ctx_token.set(token)
    scope = AUTH_SCOPE_PUBLIC
    if identity_trusted:
        scope = AUTH_SCOPE_WORKSPACE
    elif token_cid:
        scope = AUTH_SCOPE_PROJECT
    _ctx_auth.set({"token": token, "rebound": False, "scope": scope})

    if identity_trusted:
        try:
            _rehydrate_session_projects(_SESSIONS[sid])
        except Exception:
            pass

    if token:
        try:
            # 仅当本会话确为该项目所有者（已验证工作区身份）时，才按 owner 展开兄弟项目。
            # 项目 Token 单独登录不得 rehydrate 同一所有者的其他项目。
            same_owner = False
            if token_cid and token_cid in _CASES:
                same_owner = getattr(_CASES[token_cid], "owner_session", None) == sid
            rehydrate = bool(identity_trusted and same_owner)
            authorize_and_activate(
                token,
                sid,
                rehydrate_owner_projects=rehydrate,
                attach_only=bool(identity_trusted and not same_owner and cookie_verified is True),
            )
        except PermissionError:
            pass
    return sid


def _session_can_access(case_id: str, case: CaseState | None = None, *, sess: dict[str, Any] | None = None) -> bool:
    """私人项目访问：仅信任本进程已登记的 owned_case_ids 或校验通过的项目 Token。
    客户端自填 X-Session-Id 与库内 owner_session 字符串相等，不足以成为身份证明。
    """
    sess = sess or _session()
    if case_id == JINHUI_ID:
        return True
    if case_id in (sess.get('owned_case_ids') or set()):
        return True
    token = _ctx_token.get() or sess.get('project_token')
    if token and _TOKENS.get(token) == case_id:
        return True
    return False


def current_session_id() -> str:
    sid = _ctx_session.get()
    if not sid:
        sid = set_request_context(None)
    return sid


def _session() -> dict[str, Any]:
    sid = current_session_id()
    with _LOCK:
        if sid not in _SESSIONS:
            _SESSIONS[sid] = _blank_session(sid)
        else:
            _touch_session(_SESSIONS[sid])
        return _SESSIONS[sid]


def _case_id_for_token(token: str | None) -> str | None:
    """请求级 Token 对应的项目；不依赖可被其他请求改写的会话 active_case。"""
    if not token:
        return None
    with _LOCK:
        return _TOKENS.get(token)


def get_active_id() -> str:
    cid = _case_id_for_token(_ctx_token.get())
    if cid:
        return cid
    return _session()['active_case_id']


def list_cases() -> list[dict[str, Any]]:
    sess = _session()
    # 多企业列表仅反映本进程已授权（创建或 Token 校验后 rehydrate）的项目
    out = []
    # 公开示范始终可见
    demo = _DEMO or _jinhui_case()
    out.append({
        'case_id': demo.case_id,
        'name': demo.name,
        'is_demo': True,
        'protected': True,
        'data_grade': demo.data_grade,
        'version_label': demo.version_label,
        'active': sess['active_case_id'] == demo.case_id,
        'owned': False,
    })
    with _LOCK:
        for cid in list(sess['owned_case_ids']):
            c = _CASES.get(cid)
            if not c:
                continue
            out.append({
                'case_id': c.case_id,
                'name': c.name,
                'is_demo': False,
                'protected': False,
                'data_grade': c.data_grade,
                'version_label': c.version_label,
                'active': c.case_id == sess['active_case_id'],
                'owned': True,
            })
    return out


def get_case(case_id: str | None = None) -> CaseState:
    cid = case_id or get_active_id()
    if cid == JINHUI_ID:
        return deepcopy(_DEMO or _jinhui_case())
    with _LOCK:
        if cid not in _CASES:
            raise KeyError(f'案例不存在: {cid}')
        case = _CASES[cid]
        sess = _session()
        if not _session_can_access(cid, case, sess=sess):
            raise PermissionError('无权访问该项目')
        return deepcopy(case)


def _new_token() -> str:
    return secrets.token_urlsafe(24)


def _register_case(case: CaseState, session_id: str) -> CaseState:
    if not case.access_token:
        case.access_token = _new_token()
    case.owner_session = session_id
    case.protected = False
    case.is_demo = False
    case.data_prep = build_data_prep(case)
    with _LOCK:
        if session_id not in _SESSIONS:
            raise RuntimeError('会话不存在，无法登记项目')
        sess = _SESSIONS[session_id]
        prev_owned = set(sess.get('owned_case_ids') or set())
        prev_active = sess.get('active_case_id')
        prev_token = sess.get('project_token')
        previous_case = deepcopy(_CASES[case.case_id]) if case.case_id in _CASES else None
        previous_token_owner = _TOKENS.get(case.access_token)
        # 先安全落盘，再发布到内存与会话，避免 HTTP 500 却留下幽灵项目。
        try:
            _persist_case(case)
            _CASES[case.case_id] = case
            _TOKENS[case.access_token] = case.case_id
            sess.setdefault('owned_case_ids', set()).add(case.case_id)
            sess['active_case_id'] = case.case_id
            sess['project_token'] = case.access_token
            bind_request_token(case.access_token)
        except Exception:
            if previous_case is not None:
                _CASES[case.case_id] = previous_case
            else:
                _CASES.pop(case.case_id, None)
            if previous_token_owner is not None:
                _TOKENS[case.access_token] = previous_token_owner
            elif case.access_token in _TOKENS and _TOKENS.get(case.access_token) == case.case_id:
                _TOKENS.pop(case.access_token, None)
            sess['owned_case_ids'] = prev_owned
            sess['active_case_id'] = prev_active
            sess['project_token'] = prev_token
            raise
    return case


def set_active(case_id: str) -> CaseState:
    sess = _session()
    if case_id == JINHUI_ID:
        sess['active_case_id'] = JINHUI_ID
        sess['project_token'] = None
        bind_request_token(None)
        return get_case(JINHUI_ID)
    with _LOCK:
        if case_id not in _CASES:
            raise KeyError(f'案例不存在: {case_id}')
        case = _CASES[case_id]
        if not _session_can_access(case_id, case, sess=sess):
            raise PermissionError('无权激活他人项目')
        sess['active_case_id'] = case_id
        sess['project_token'] = case.access_token
        bind_request_token(case.access_token)
        return deepcopy(case)


def authorize_and_activate(
    token: str,
    session_id: str | None = None,
    *,
    rehydrate_owner_projects: bool = False,
    attach_only: bool = False,
) -> CaseState:
    """用项目 Token 授权对应的那一个案例。

    默认不把同一所有者的兄弟项目加入工作区（项目 Token ≠ 工作区凭证）。
    仅当调用方已持有已验证的所有者工作区会话，并显式传入
    rehydrate_owner_projects=True 时，才恢复该所有者的全部项目。
    attach_only=True：本请求可通过 Token 访问该案例，但不把该项目并入当前会话名下。
    """
    sid = session_id or current_session_id()
    with _LOCK:
        cid = _TOKENS.get(token)
        if not cid or cid not in _CASES:
            raise PermissionError('无效项目凭证')
        case = _CASES[cid]
        sess = _SESSIONS.setdefault(sid, _blank_session(sid))
        _touch_session(sess)
        if attach_only:
            bind_request_token(token)
            return deepcopy(case)
        sess['owned_case_ids'].add(cid)
        if rehydrate_owner_projects:
            owner = getattr(case, 'owner_session', None)
            _rehydrate_session_projects(sess, owner_sid=owner or sid)
        sess['active_case_id'] = cid
        sess['project_token'] = token
        bind_request_token(token)
        return deepcopy(case)


def fork_writable(base: CaseState | None = None, name_suffix: str = '（可编辑副本）') -> CaseState:
    """从当前或给定案例 fork 可写副本到本会话。"""
    src = base or get_case()
    sid = current_session_id()
    state = deepcopy(src)
    origin = src.case_id
    state.case_id = f'user-{uuid.uuid4().hex[:8]}'
    state.is_demo = False
    state.protected = False
    state.origin_case_id = getattr(src, 'origin_case_id', None) or origin
    if name_suffix and not state.name.endswith(name_suffix):
        state.name = (state.name or '用户案例') + name_suffix
    state.version_label = 'fork'
    state.access_token = _new_token()
    return _register_case(state, sid)


def empty_enterprise_case(name: str = '新企业项目') -> CaseState:
    """新企业：不隐式继承金徽 200kW / 12.93% / 投资假设。"""
    sid = current_session_id()
    state = CaseState(
        case_id=f'user-{uuid.uuid4().hex[:8]}',
        name=name,
        is_demo=False,
        protected=False,
        data_grade='annual',
        energy_years=[],
        devices=[],
        retrofits=[],
        scenario=ScenarioParams(
            power_drop_kw=None,
            hours=None,
            baseline_power_kw=None,
            pump_baseline_kwh=None,
            energy_price=None,
            emission_factor=0.4471,
            investment_cny=None,
            annual_maintenance_cny=None,
            assume_zero_maintenance=False,
            use_demo_baseline=False,
            use_jinhui_reference=False,
            use_jinhui_tariff_reference=False,
            selected_measure_id=None,
            emission_factor_user_override=False,
            energy_price_user_override=False,
        ),
        sources=[],
        version_label='upload',
        access_token=_new_token(),
    )
    return _register_case(state, sid)


def replace_active_working_copy(state: CaseState) -> CaseState:
    """导入后写入：若当前是示范则 fork；否则覆盖本会话项目。"""
    sess = _session()
    active_id = get_active_id()
    sid = sess['session_id']
    if active_id == JINHUI_ID:
        state.case_id = f'user-{uuid.uuid4().hex[:8]}'
        state.is_demo = False
        state.protected = False
        state.name = state.name or f'用户案例-{state.case_id}'
        state.access_token = _new_token()
        state.revision = 1
        return _register_case(state, sid)
    with _LOCK:
        old = _CASES.get(active_id)
        if not old or not _session_can_access(active_id, old, sess=sess):
            state.case_id = f'user-{uuid.uuid4().hex[:8]}'
            state.access_token = _new_token()
            state.revision = 1
            return _register_case(state, sid)
        incoming = int(getattr(state, 'revision', 0) or 0)
        old_rev = int(getattr(old, 'revision', 0) or 0)
        if incoming != old_rev:
            raise VersionConflict('版本已变化，请刷新后重试')
        state.case_id = active_id
        state.protected = False
        state.is_demo = False
        state.access_token = old.access_token
        state.owner_session = old.owner_session or sid
        state.revision = old_rev + 1
        state.data_prep = build_data_prep(state)
        previous = deepcopy(old) if old is not None else None
        _CASES[active_id] = state
        sess['project_token'] = state.access_token
        sess['active_case_id'] = active_id
        try:
            _persist_case(state)
        except Exception:
            if previous is not None:
                _CASES[active_id] = previous
            else:
                _CASES.pop(active_id, None)
            raise
    return state


def update_case(mutator: Callable[[CaseState], None], *, trial_only: bool = False) -> CaseState:
    """复制→修改；默认提交。整个读改写持锁，避免并发 200 后丢写入。"""
    with _LOCK:
        case = get_case()
        if case.protected or case.case_id == JINHUI_ID:
            case.case_id = f'user-{uuid.uuid4().hex[:8]}'
            case.is_demo = False
            case.protected = False
            case.name = case.name + '（可编辑副本）'
            case.origin_case_id = case.origin_case_id or JINHUI_ID
            case.version_label = 'fork-from-demo'
            case.access_token = _new_token()
            case.revision = 0
            mutator(case)
            case.data_prep = build_data_prep(case)
            if trial_only:
                return case
            return _register_case(case, current_session_id())
        mutator(case)
        case.data_prep = build_data_prep(case)
        if trial_only:
            return case
        return commit_case(case)


def commit_case(case: CaseState) -> CaseState:
    """将已通过校验的案例副本提交到当前会话；过期草稿 409，禁止静默覆盖。"""
    sid = current_session_id()
    if case.case_id == JINHUI_ID or case.protected:
        case.case_id = f'user-{uuid.uuid4().hex[:8]}'
        case.is_demo = False
        case.protected = False
        if '可编辑副本' not in (case.name or ''):
            case.name = (case.name or '用户案例') + '（可编辑副本）'
        case.origin_case_id = case.origin_case_id or JINHUI_ID
        case.access_token = case.access_token or _new_token()
        case.revision = (getattr(case, 'revision', 0) or 0) + 1
        return _register_case(case, sid)
    case.data_prep = build_data_prep(case)
    with _LOCK:
        if case.case_id not in _CASES:
            case.access_token = case.access_token or _new_token()
            case.revision = (getattr(case, 'revision', 0) or 0) + 1
            return _register_case(case, sid)
        old = _CASES.get(case.case_id)
        if old is not None:
            old_rev = int(getattr(old, 'revision', 0) or 0)
            incoming = int(getattr(case, 'revision', 0) or 0)
            if incoming != old_rev:
                raise VersionConflict('版本已变化，请刷新后重试')
            if not case.access_token:
                case.access_token = old.access_token
            if not case.owner_session:
                case.owner_session = old.owner_session
            case.revision = old_rev + 1
        else:
            case.revision = (getattr(case, 'revision', 0) or 0) + 1
        previous = deepcopy(old) if old is not None else None
        _CASES[case.case_id] = case
        sess = _session()
        sess['active_case_id'] = case.case_id
        sess['project_token'] = case.access_token
        sess['owned_case_ids'].add(case.case_id)
        if case.access_token:
            _TOKENS[case.access_token] = case.case_id
        bind_request_token(case.access_token)
        try:
            _persist_case(case)
        except Exception:
            if previous is not None:
                _CASES[case.case_id] = previous
            else:
                _CASES.pop(case.case_id, None)
            raise
    return case


def save_version(label: str | None = None) -> dict:
    case = get_case()
    if case.case_id == JINHUI_ID:
        # 示范只读：先提示性保存为会话副本版本
        pass
    token = case.access_token or _session().get('project_token') or 'demo'
    # 无权写他人
    sess = _session()
    if case.case_id != JINHUI_ID:
        if not _session_can_access(case.case_id, case, sess=sess):
            raise PermissionError('无权保存该项目版本')
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    vid = f'{case.case_id}_{ts}_{uuid.uuid4().hex[:8]}'
    label = label or ts
    payload = case.model_dump()
    payload['saved_label'] = label
    payload['saved_at'] = ts
    with _LOCK:
        conn = _db()
        try:
            conn.execute(
                '''INSERT INTO versions(version_id, case_id, token, owner_session, label, payload, saved_at)
                   VALUES(?,?,?,?,?,?,?)''',
                (vid, case.case_id, token or '', sess['session_id'], label,
                 json.dumps(payload, ensure_ascii=False), ts),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.close()
            raise ValueError(f'保存版本失败：编号冲突，请重试（{vid}）') from exc
        conn.close()
    # 兼容旧文件接口：JSON 为可选备份。主存储是 SQLite，备份失败不得把已成功的主事务报成失败。
    path = VERSIONS / f'{vid}.json'
    backup_ok = True
    warning = None
    try:
        VERSIONS.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as exc:
        backup_ok = False
        warning = f'版本已写入主数据库，但兼容 JSON 备份未保存（{exc}）。列表与恢复以数据库为准。'
        path = None
    result = {
        'version_id': vid,
        'label': label,
        'sqlite_saved': True,
        'backup_ok': backup_ok,
        'path': str(path) if path is not None else None,
    }
    if warning:
        result['warning'] = warning
    return result


def list_versions() -> list[dict]:
    """仅列出本会话有权访问的版本。"""
    sess = _session()
    owned = set(sess['owned_case_ids'])
    items = []
    with _LOCK:
        conn = _db()
        rows = conn.execute(
            'SELECT version_id, label, case_id, saved_at, owner_session, token FROM versions ORDER BY saved_at'
        ).fetchall()
        conn.close()
        for vid, label, case_id, saved_at, owner_session, token in rows:
            # 必须是「当前请求持有」的凭证，不能仅因库内 token 字符串本身有效就放行
            req_tok = _ctx_token.get() or sess.get('project_token')
            tok_ok = bool(
                req_tok
                and (
                    req_tok == token
                    or _TOKENS.get(req_tok) == case_id
                )
            )
            if case_id in owned or tok_ok:
                items.append({
                    'version_id': vid,
                    'label': label,
                    'case_id': case_id,
                    'saved_at': saved_at,
                })
    return items


def restore_version(version_id: str) -> CaseState:
    sess = _session()
    with _LOCK:
        conn = _db()
        row = conn.execute(
            'SELECT payload, case_id, owner_session, token FROM versions WHERE version_id=?',
            (version_id,),
        ).fetchone()
        conn.close()
    if not row:
        # 兼容旧文件
        path = VERSIONS / f'{version_id}.json'
        if not path.exists():
            raise FileNotFoundError('版本不存在')
        data = json.loads(path.read_text(encoding='utf-8'))
        # 仅当属于本会话案例前缀时允许（防越权）
        cid = data.get('case_id')
        if cid not in sess['owned_case_ids'] and cid != JINHUI_ID:
            raise PermissionError('无权恢复该版本')
    else:
        payload, case_id, owner_session, token = row
        req_tok = _ctx_token.get() or sess.get('project_token')
        tok_ok = bool(req_tok and (req_tok == token or _TOKENS.get(req_tok) == case_id))
        if case_id not in sess['owned_case_ids'] and not tok_ok:
            raise PermissionError('无权恢复该版本')
        data = json.loads(payload)
    state = CaseState.model_validate(data)
    if state.case_id == JINHUI_ID or state.protected:
        state.case_id = f'user-{uuid.uuid4().hex[:8]}'
        state.protected = False
        state.is_demo = False
        state.name = (state.name or '恢复版本') + '（恢复）'
        state.access_token = _new_token()
    # 恢复必须成为新的合法修订并真正落盘，禁止把修订号倒退导致 _persist_case 静默跳过
    with _LOCK:
        existing = _CASES.get(state.case_id)
        db_rev = int(getattr(existing, 'revision', 0) or 0) if existing else 0
        conn = _db()
        row = conn.execute('SELECT payload FROM projects WHERE case_id=?', (state.case_id,)).fetchone()
        conn.close()
        if row:
            db_rev = max(db_rev, _payload_revision(row[0]))
        snap_rev = int(getattr(state, 'revision', 0) or 0)
        state.revision = max(db_rev, snap_rev) + 1
        if existing:
            state.access_token = existing.access_token or state.access_token
            state.owner_session = existing.owner_session or state.owner_session
            state.case_id = existing.case_id
        if not state.access_token:
            state.access_token = _new_token()
        state.data_prep = build_data_prep(state)
        registered = _register_case(state, sess['session_id'])
        conn = _db()
        check = conn.execute('SELECT payload FROM projects WHERE case_id=?', (registered.case_id,)).fetchone()
        conn.close()
        if not check:
            raise RuntimeError('恢复版本未能写入数据库')
        durable = json.loads(check[0])
        if int(durable.get('revision') or 0) != int(registered.revision):
            raise RuntimeError('恢复版本未能持久化：修订号未写入')
        return registered


def compare_versions(a_id: str, b_id: str) -> dict:
    def load(vid: str) -> dict:
        with _LOCK:
            conn = _db()
            row = conn.execute(
                'SELECT payload, case_id, owner_session, token FROM versions WHERE version_id=?',
                (vid,),
            ).fetchone()
            conn.close()
        sess = _session()
        if row:
            payload, case_id, owner_session, token = row
            req_tok = _ctx_token.get() or sess.get('project_token')
            tok_ok = bool(req_tok and (req_tok == token or _TOKENS.get(req_tok) == case_id))
            if case_id not in sess['owned_case_ids'] and not tok_ok:
                raise PermissionError(vid)
            return json.loads(payload)
        p = VERSIONS / f'{vid}.json'
        if not p.exists():
            raise FileNotFoundError(vid)
        data = json.loads(p.read_text(encoding='utf-8'))
        if data.get('case_id') not in sess['owned_case_ids'] and data.get('case_id') != JINHUI_ID:
            raise PermissionError(vid)
        return data
    a, b = load(a_id), load(b_id)
    return {
        'a': {'version_id': a_id, 'name': a.get('name'), 'scenario': a.get('scenario'),
              'device_count': len(a.get('devices') or []), 'energy_years': len(a.get('energy_years') or [])},
        'b': {'version_id': b_id, 'name': b.get('name'), 'scenario': b.get('scenario'),
              'device_count': len(b.get('devices') or []), 'energy_years': len(b.get('energy_years') or [])},
    }


def infer_data_grade(case: CaseState) -> DataGrade:
    has_device = any(
        (d.power_kw is not None and d.power_kw > 0)
        or (d.baseline_kwh is not None and d.baseline_kwh > 0)
        for d in case.devices
    )
    if case.scenario.baseline_power_kw is not None or case.scenario.pump_baseline_kwh is not None:
        has_device = True
    if case.data_grade == 'hourly':
        return 'hourly'
    return 'device' if has_device else 'annual'


def reset_session_demo() -> CaseState:
    """仅重置当前会话到公开示范，不影响其他会话。"""
    sess = _session()
    sess['active_case_id'] = JINHUI_ID
    sess['project_token'] = None
    bind_request_token(None)
    return get_case(JINHUI_ID)


def rotate_project_token(case_id: str | None = None) -> str:
    """轮换当前（或指定）项目的访问 Token：旧 Token 立即失效，返回新 Token。

    需要已对该项目拥有访问权（工作区会话或仍有效的项目 Token）。
    公开示范不可轮换。退出工作区不会自动调用本函数。
    """
    case = get_case(case_id)
    if case.case_id == JINHUI_ID or case.protected or case.is_demo:
        raise PermissionError("公开示范没有可轮换的项目凭证")
    new_tok = _new_token()
    with _LOCK:
        live = _CASES.get(case.case_id)
        if live is None:
            raise KeyError(f"案例不存在: {case.case_id}")
        old = live.access_token
        previous = deepcopy(live)
        live.access_token = new_tok
        try:
            if old:
                _TOKENS.pop(old, None)
            _TOKENS[new_tok] = case.case_id
            _persist_case(live)
        except Exception:
            live.access_token = previous.access_token
            if old:
                _TOKENS[old] = case.case_id
            _TOKENS.pop(new_tok, None)
            raise
        for sess in _SESSIONS.values():
            if old and sess.get("project_token") == old:
                sess["project_token"] = new_tok
        sess = _SESSIONS.get(current_session_id())
        if sess is not None:
            sess["project_token"] = new_tok
        bind_request_token(new_tok)
    return new_tok


def revoke_project_token(case_id: str | None = None) -> None:
    """撤销当前项目已发放的访问 Token，不把新凭证交给调用方。

    仍可通过已验证的工作区会话访问该项目，并可再次轮换得到新凭证。
    仅持有旧 Token、没有工作区会话的调用方在撤销后将失去访问权。
    公开示范不可撤销。
    """
    case = get_case(case_id)
    if case.case_id == JINHUI_ID or case.protected or case.is_demo:
        raise PermissionError("公开示范没有可撤销的项目凭证")
    workspace = current_auth_scope() == AUTH_SCOPE_WORKSPACE
    new_tok = _new_token()
    with _LOCK:
        live = _CASES.get(case.case_id)
        if live is None:
            raise KeyError(f"案例不存在: {case.case_id}")
        old = live.access_token
        previous = deepcopy(live)
        live.access_token = new_tok
        try:
            if old:
                _TOKENS.pop(old, None)
            _TOKENS[new_tok] = case.case_id
            _persist_case(live)
        except Exception:
            live.access_token = previous.access_token
            if old:
                _TOKENS[old] = case.case_id
            _TOKENS.pop(new_tok, None)
            raise
        for sess in _SESSIONS.values():
            if old and sess.get("project_token") == old:
                sess["project_token"] = new_tok if workspace else None
        if workspace:
            sess = _SESSIONS.get(current_session_id())
            if sess is not None:
                sess["project_token"] = new_tok
            bind_request_token(new_tok)
        else:
            sess = _SESSIONS.get(current_session_id())
            if sess is not None:
                sess["project_token"] = None
                if case.case_id in (sess.get("owned_case_ids") or set()):
                    sess["owned_case_ids"].discard(case.case_id)
                if sess.get("active_case_id") == case.case_id:
                    sess["active_case_id"] = JINHUI_ID
            bind_request_token(None)


def bind_request_token(token: str | None) -> None:
    """把当前请求上下文绑定到新项目凭证；切换/新建/回示范后必须调用，避免旧 Token 继续主导 get_active_id。"""
    _ctx_token.set(token)
    holder = _ctx_auth.get()
    if holder is None:
        holder = {'token': token, 'rebound': True}
        _ctx_auth.set(holder)
    else:
        holder['token'] = token
        holder['rebound'] = True


def session_project_token() -> str | None:
    holder = _ctx_auth.get()
    if holder and holder.get('rebound'):
        return holder.get('token')
    tok = _ctx_token.get()
    if tok and _case_id_for_token(tok):
        return tok
    return _session().get('project_token')


# 启动初始化
init_store()
