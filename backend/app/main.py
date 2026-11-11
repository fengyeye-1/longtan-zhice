from __future__ import annotations
import json
import math
import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, File, UploadFile, HTTPException, Query, Request, Response, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from urllib.parse import urlparse
from pydantic import ValidationError

from .services import store
from .services import equipment_catalog as equip_cat
from .calc import physics_models as phys
from .services.templates_xlsx import template_path, build_templates, STANDARD_FIELDS
from .services.import_data import (
    read_table, preview_dataframe, apply_import, check_upload_limits, check_upload_rate,
    MAX_UPLOAD_BYTES,
)
from .services.compute import (
    full_recalculate, recalculate_case, build_report_text, compare_measures, sanitize_csv_cell, render_csv_rows,
)
from .services import procurement_catalog as proc_cat
from .services import report_sections as report_sec
from .services.change_validation import (
    ChangeRejected, validate_and_apply_retrofit_update, on_device_type_changed,
    on_measure_deleted, on_device_deleted, retire_tariff_quote, retire_device_quotes,
    prepare_copied_measure, add_copied_provenance,
    restore_measure_refs, snapshot_measure_refs,
)
from .models.schema import (
    FieldMapping, AddDeviceRequest, UpdateDeviceRequest, AddRetrofitRequest,
    UpdateScenarioRequest, CustomParamRequest, CustomParam, Device, RetrofitProject,
    CompareSchemesRequest, CaseState, ApplyQuoteRequest, UserQuoteRequest,
    ReportExportRequest, QuoteChoice, ScenarioParams,
)
from .calc.equipment import MAX_ANNUAL_HOURS

app = FastAPI(title='陇碳智策 API', version='2.0.0')


def _http_store(e: Exception) -> HTTPException:
    if isinstance(e, HTTPException):
        return e
    if isinstance(e, store.VersionConflict) or getattr(e, 'status_code', None) == 409:
        return HTTPException(status_code=409, detail=str(e))
    if isinstance(e, PermissionError):
        return HTTPException(status_code=403, detail=str(e))
    return HTTPException(status_code=400, detail=str(e))


def _assert_results_finite(results: dict) -> None:
    sa = (results or {}).get('selected_assessment') or {}
    for key in ('saved_kwh', 'co2_reduction_t', 'annual_cost_saving_cny', 'annual_net_saving_cny', 'simple_payback_years'):
        val = sa.get(key)
        if isinstance(val, (int, float)) and not math.isfinite(float(val)):
            raise HTTPException(400, '计算结果超出有限范围，未保存本次输入')


def _sanitize_user_data_type(raw: str | None, *, is_demo: bool) -> str:
    """V8 P0-06：非示范企业不得自行升为实测/披露/文献认证身份。"""
    if is_demo:
        return raw or '披露'
    trusted = {'实测', '披露', '文献'}
    if not raw or raw in trusted:
        return '用户提供·未核验'
    if raw in ('用户提供·未核验', '企业提供·未核验', '假设', '模型估算'):
        return raw
    return '用户提供·未核验'



# CORS：仅允许 LTZS_CORS_ORIGINS 中的显式来源；不匹配则绝不返回带凭证的跨域许可。
# 默认只开放本机一键启动与 Vite 开发端口，不再用正则放行任意 localhost 端口。
_cors = os.environ.get('LTZS_CORS_ORIGINS', 'http://127.0.0.1:8000,http://localhost:8000,http://127.0.0.1:5173,http://localhost:5173')
_origins = [o.strip() for o in _cors.split(',') if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins if _origins != ['*'] else [],
    allow_credentials=True if _origins and _origins != ['*'] else False,
    allow_methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
    allow_headers=['*'],
)

build_templates()
store.init_store()



# CSRF / Origin 防护：CORS 只约束带凭证的跨域 *读取*，不能代替改变状态的 Origin 校验。
# 策略：
# 1) 安全方法（GET/HEAD/OPTIONS）不检查。
# 2) 若存在 Origin，必须属于 CORS 白名单或 LTZS_PUBLIC_ORIGIN，或与本请求的 scheme+host+port 完全同源（本机一键启动、局域网 IP 直开）。
#    若 Origin 的主机名已出现在上述配置来源中，但 scheme+host+port 并未精确命中白名单，则直接拒绝，
#    不再回退到 request.url.scheme（TLS 终止后容器可能只看到 HTTP，否则会把 http://同一公网主机 误判为同源）。
#    不要用客户端可伪造的 X-Forwarded-Proto 判断 CSRF 来源。
# 3) 若存在 Referer 而无 Origin，按 Referer 的 origin 执行同样规则。
# 4) 无 Origin、无 Referer：视为非浏览器 API 客户端（pytest、本地脚本）。
#    浏览器跨站表单会带 Origin；不把「无 Origin」当成匿名浏览器绕过，因为现代浏览器跨站 POST 必带 Origin。
#    一键启动只打开同源页面，fetch 会带白名单 Origin，不会走此分支。
_CSRF_SAFE = {'GET', 'HEAD', 'OPTIONS'}
_CSRF_SIMPLE_TYPES = {'application/x-www-form-urlencoded', 'text/plain', 'multipart/form-data'}


def _request_origin(request: Request) -> str | None:
    origin = (request.headers.get('origin') or '').strip()
    if origin:
        return origin
    referer = (request.headers.get('referer') or '').strip()
    if not referer:
        return None
    try:
        u = urlparse(referer)
        if u.scheme and u.netloc:
            return f"{u.scheme}://{u.netloc}"
    except Exception:
        return None
    return None


def _configured_trusted_origins() -> set[str]:
    """CORS 白名单 + 显式配置的公网来源。每次请求读取环境变量，便于测试与部署切换。"""
    allowed = {o.strip().rstrip('/') for o in _origins if o}
    public = os.environ.get('LTZS_PUBLIC_ORIGIN', '').strip().rstrip('/')
    if public:
        allowed.add(public)
    return allowed


def _origin_is_trusted(origin: str, request: Request) -> bool:
    """同源或白名单来源才允许改变状态。比较完整的 scheme+host+port。

    反向代理场景请设置 LTZS_PUBLIC_ORIGIN=https://公开域名 ，不要信任
    客户端可伪造的 X-Forwarded-Proto。本机一键启动的 HTTP 同源仍然有效。

    若 Origin 主机名已出现在配置的可信来源中，但精确的 scheme+host+port
    未命中白名单，则拒绝，不再用 request.url.scheme 做同源回退。这样在
    TLS 终止后容器只看到 HTTP 时，也不会把 http://同一公网主机 放行。
    """
    origin = (origin or '').strip()
    if not origin:
        return False
    allowed = _configured_trusted_origins()
    if origin.rstrip('/') in allowed:
        return True
    parsed = urlparse(origin)
    scheme = (parsed.scheme or '').lower()
    netloc = (parsed.netloc or '').lower()
    if scheme not in ('http', 'https') or not netloc:
        return False
    try:
        origin_host = (parsed.hostname or '').lower()
    except ValueError:
        return False
    if origin_host:
        configured_hosts: set[str] = set()
        for item in allowed:
            try:
                host_i = urlparse(item).hostname
            except ValueError:
                host_i = None
            if host_i:
                configured_hosts.add(host_i.lower())
        if origin_host in configured_hosts:
            # 主机名已在白名单/公网来源中，但精确 origin 未命中：不得回退到请求 scheme。
            return False
    host = (request.headers.get('host') or '').strip().lower()
    if not host:
        host = (request.url.netloc or '').lower()
    req_scheme = (request.url.scheme or '').lower()
    if req_scheme not in ('http', 'https') or not host:
        return False
    return f'{scheme}://{netloc}' == f'{req_scheme}://{host}'


def _csrf_guard(request: Request) -> JSONResponse | None:
    if not request.url.path.startswith('/api'):
        return None
    if request.method.upper() in _CSRF_SAFE:
        return None
    origin = _request_origin(request)
    if origin:
        if _origin_is_trusted(origin, request):
            return None
        return JSONResponse(
            status_code=403,
            content={'detail': '拒绝非可信来源的状态修改请求（Origin/CSRF 校验失败）'},
        )
    # 无 Origin/Referer：允许非浏览器 API 客户端。不额外要求自定义头，以免误伤 pytest 与本地启动器。
    return None


def _cookie_secure(request: Request | None = None) -> bool:
    env = os.environ.get('LTZS_COOKIE_SECURE', '').strip().lower()
    if env in ('1', 'true', 'yes', 'on'):
        return True
    if env in ('0', 'false', 'no', 'off'):
        return False
    if request is None:
        return False
    if request.url.scheme == 'https':
        return True
    xf = (request.headers.get('x-forwarded-proto') or '').split(',')[0].strip().lower()
    return xf == 'https'


def _cookie_kwargs(request: Request | None = None) -> dict:
    return {
        'httponly': True,
        'samesite': 'lax',
        'max_age': 86400 * 7,
        'path': '/',
        'secure': _cookie_secure(request),
    }


def _attach_session_cookies(response: Response, sid: str, request: Request | None = None, mac: str | None = None) -> None:
    kw = _cookie_kwargs(request)
    if mac:
        stk = store.maybe_rotate_session_mac(sid, mac)
    else:
        stk = store.issue_session_mac(sid)
    response.set_cookie('ltzs_sid', sid, **kw)
    response.set_cookie('ltzs_stk', stk, **kw)
    response.headers['X-Session-Id'] = sid


def _clear_session_cookies(response: Response, request: Request | None = None) -> None:
    kw = _cookie_kwargs(request)
    # max_age=0 to expire
    expire = dict(kw)
    expire['max_age'] = 0
    response.set_cookie('ltzs_sid', '', **expire)
    response.set_cookie('ltzs_stk', '', **expire)


def _bind_session(request: Request, response: Response,
                  x_session_id: Optional[str] = None,
                  x_project_token: Optional[str] = None) -> str:
    cookie_sid = request.cookies.get('ltzs_sid')
    cookie_mac = request.cookies.get('ltzs_stk')
    header_sid = x_session_id or request.headers.get('X-Session-Id')
    token = x_project_token or request.headers.get('X-Project-Token')
    cookie_verified = store.verify_session_mac(cookie_sid, cookie_mac)
    # 未校验的 Header SID 不得作为身份传入；由 set_request_context 另签发新会话。
    sid = cookie_sid if cookie_verified else None
    sid = store.set_request_context(sid, token, cookie_verified=cookie_verified)
    _attach_session_cookies(response, sid, request, cookie_mac if cookie_verified else None)
    tok = store.session_project_token()
    if tok:
        response.headers['X-Project-Token'] = tok
    return sid


def _apply_scenario_to_case(case: CaseState, body: UpdateScenarioRequest) -> list[str]:
    """把请求应用到案例副本，返回字段级错误（不抛）。"""
    errors: list[str] = []
    data = body.model_dump(exclude_none=True)
    sc = case.scenario

    if data.pop('clear_baseline_power_kw', None):
        sc.baseline_power_kw = None
        sc.use_demo_baseline = False
    if data.pop('clear_pump_baseline_kwh', None):
        sc.pump_baseline_kwh = None
        sc.pump_baseline_bound_measure_id = None
        sc.pump_baseline_bound_device_id = None
    if data.pop('clear_investment_cny', None):
        sc.investment_cny = None
        sc.investment_bound_measure_id = None
        # 同步清空当前选中措施上的投资假设，避免仍按措施投资算出回收期
        if sc.selected_measure_id:
            m_clear = next((r for r in case.retrofits if r.id == sc.selected_measure_id), None)
            if m_clear is not None:
                m_clear.investment_cny = None
    if data.pop('clear_power_drop_kw', None):
        sc.power_drop_kw = None
        if sc.selected_measure_id:
            m_pd = next((r for r in case.retrofits if r.id == sc.selected_measure_id), None)
            if m_pd is not None:
                m_pd.power_drop_kw = None
    if data.pop('clear_hours', None):
        sc.hours = None
    if data.pop('clear_energy_price', None):
        sc.energy_price = None
        sc.energy_price_user_override = True
        sc.use_jinhui_tariff_reference = False
        retire_tariff_quote(case, '用户清空电价')
    if data.pop('clear_annual_maintenance_cny', None):
        sc.annual_maintenance_cny = None
        sc.maintenance_bound_measure_id = None
        sc.assume_zero_maintenance = False
        if sc.selected_measure_id:
            m_om = next((r for r in case.retrofits if r.id == sc.selected_measure_id), None)
            if m_om is not None:
                m_om.annual_maintenance_cny = None

    use_demo = data.pop('use_demo_baseline', None)
    if use_demo is not None:
        sc.use_demo_baseline = use_demo
        if use_demo and sc.baseline_power_kw is None:
            sc.baseline_power_kw = 2400.0
        if not use_demo and sc.baseline_power_kw == 2400.0:
            sc.baseline_power_kw = None

    if 'use_jinhui_reference' in data:
        sc.use_jinhui_reference = bool(data.pop('use_jinhui_reference'))
        if sc.use_jinhui_reference:
            # 显式参考：仅填充技改情景参数；电价须另勾选 use_jinhui_tariff_reference
            if sc.power_drop_kw is None:
                sc.power_drop_kw = 200
            if sc.hours is None:
                sc.hours = 4000
            if sc.investment_cny is None:
                sc.investment_cny = 1_000_000
            if sc.selected_measure_id is None and case.retrofits:
                mill_rf = next((r for r in case.retrofits if r.power_drop_kw is not None), None)
                if mill_rf:
                    sc.selected_measure_id = mill_rf.id

    if 'use_jinhui_tariff_reference' in data:
        sc.use_jinhui_tariff_reference = bool(data.pop('use_jinhui_tariff_reference'))
        if sc.use_jinhui_tariff_reference:
            from .calc.equipment import COST_INTENSITY_CNY_PER_KWH
            # 参考方案与手动覆盖互斥：开启参考即改用参考电价，并清除手动覆盖标志。
            sc.energy_price = COST_INTENSITY_CNY_PER_KWH
            sc.energy_price_user_override = False
        else:
            from .calc.equipment import COST_INTENSITY_CNY_PER_KWH
            if (
                not sc.energy_price_user_override
                and sc.energy_price is not None
                and abs(float(sc.energy_price) - COST_INTENSITY_CNY_PER_KWH) < 1e-9
                and not case.is_demo
            ):
                sc.energy_price = None

    if 'assume_zero_maintenance' in data:
        # 临时假设不得覆盖措施上的永久维护费（含未知）。计算层按旗标把未知当作 0 试算。
        sc.assume_zero_maintenance = bool(data.pop('assume_zero_maintenance'))

    if 'selected_measure_id' in data:
        mid = data.pop('selected_measure_id')
        sc.selected_measure_id = mid
        m = next((r for r in case.retrofits if r.id == mid), None)
        if m is None:
            errors.append('selected_measure_id: 措施不存在')
        else:
            if m.device_id and not any(d.id == m.device_id for d in case.devices):
                errors.append(f'selected_measure_id: 措施未绑定到有效设备 {m.device_id}')
            # 按措施模型更新功率降幅；比例法清空，避免旧球磨 200kW 残留
            method = getattr(m, 'calculation_method', None)
            if method == 'baseline_fraction' or (
                m.saving_fraction is not None and m.power_drop_kw is None
            ):
                sc.power_drop_kw = None
            elif m.power_drop_kw is not None:
                sc.power_drop_kw = m.power_drop_kw
                # P0-05：切换时也校验功率降幅 vs 设备功率
                if m.device_id:
                    dchk = next((d for d in case.devices if d.id == m.device_id), None)
                    if (
                        dchk is not None and dchk.power_kw is not None
                        and float(m.power_drop_kw) > float(dchk.power_kw)
                    ):
                        errors.append(
                            f'selected_measure_id: 功率降幅 {m.power_drop_kw} kW 大于设备功率 '
                            f'{dchk.power_kw} kW'
                        )
            if m.investment_cny is not None:
                sc.investment_cny = m.investment_cny
                sc.investment_bound_measure_id = m.id
            else:
                sc.investment_cny = None
                sc.investment_bound_measure_id = None
            if m.annual_maintenance_cny is not None:
                sc.annual_maintenance_cny = m.annual_maintenance_cny
                sc.maintenance_bound_measure_id = m.id
            else:
                sc.annual_maintenance_cny = None
                sc.maintenance_bound_measure_id = m.id
            # P0-11：报价绑定措施/设备；切换后不匹配则解除
            qc = case.quote_choice
            if qc is not None:
                qm = getattr(qc, 'measure_id', None)
                qd = getattr(qc, 'device_id', None)
                if (qm and qm != m.id) or (qd and m.device_id and qd != m.device_id):
                    case.quote_choice = None
                elif qm is None and qd is None:
                    # 旧报价无绑定字段：切换措施后一律解除，避免球磨吃泵报价
                    case.quote_choice = None

    if 'emission_factor' in data:
        sc.emission_factor = data.pop('emission_factor')
        sc.emission_factor_user_override = True
    if 'energy_price' in data:
        sc.energy_price = data.pop('energy_price')
        sc.energy_price_user_override = True
        sc.use_jinhui_tariff_reference = False
        retire_tariff_quote(case, '用户手动修改电价')

    for k, v in data.items():
        if hasattr(sc, k):
            # 额外范围检查
            if k == 'hours' and v is not None:
                if v < 0:
                    errors.append('hours: 不得为负')
                    continue
                # 闰年允许 8784；非闰年上限 8760（与设备校验一致）
                if v > 8784:
                    errors.append('hours: 不可超过 8784')
                    continue
                # P0-03：不得在 setattr 前放过「超过设备实际运行小时」
                m_h = next((r for r in case.retrofits if r.id == sc.selected_measure_id), None)
                if m_h and m_h.device_id and m_h.power_drop_kw is not None:
                    d_h = next((d for d in case.devices if d.id == m_h.device_id), None)
                    if d_h is not None and d_h.hours is not None and float(v) > float(d_h.hours):
                        errors.append(
                            'hours: 预计节能小时超过设备实际运行时间，请重新填写'
                        )
                        continue
            if k in ('investment_cny', 'power_drop_kw', 'annual_maintenance_cny',
                     'energy_price', 'emission_factor', 'baseline_power_kw', 'pump_baseline_kwh'):
                if v is not None and v < 0:
                    errors.append(f'{k}: 不得为负')
                    continue
            if k == 'efficiency' and v is not None and not (0 <= v <= 1):
                errors.append('efficiency: 须在 0—1')
                continue
            if isinstance(v, float) and not math.isfinite(v):
                errors.append(f'{k}: 不能为 NaN/Infinity')
                continue
            setattr(sc, k, v)
            if k == 'power_drop_kw':
                if sc.selected_measure_id:
                    m2 = next((r for r in case.retrofits if r.id == sc.selected_measure_id), None)
                    if m2 is not None:
                        m2.power_drop_kw = v
            if k == 'investment_cny':
                sc.investment_bound_measure_id = sc.selected_measure_id
                # 同步回写措施投资（显式「将该投资作为该措施假设」）
                if sc.selected_measure_id and v is not None:
                    m2 = next((r for r in case.retrofits if r.id == sc.selected_measure_id), None)
                    if m2 is not None:
                        m2.investment_cny = v
            if k == 'annual_maintenance_cny':
                sc.maintenance_bound_measure_id = sc.selected_measure_id
                if sc.selected_measure_id and v is not None:
                    m2 = next((r for r in case.retrofits if r.id == sc.selected_measure_id), None)
                    if m2 is not None:
                        m2.annual_maintenance_cny = v
            if k == 'pump_baseline_kwh' and v is not None:
                # P0-01：情景「泵基准」字段只绑定到泵措施；不得绑到球磨/风机比例措施
                def _is_pump_dev(r):
                    if not r.device_id:
                        return '泵' in f"{r.name or ''}{r.target_device or ''}"
                    return any(
                        d.id == r.device_id and ('泵' in (d.device_type or '') or '泵' in (d.name or ''))
                        for d in case.devices
                    )
                m_sel = next((r for r in case.retrofits if r.id == sc.selected_measure_id), None)
                pump_m = None
                if m_sel is not None and m_sel.saving_fraction is not None and _is_pump_dev(m_sel):
                    pump_m = m_sel
                if pump_m is None:
                    pump_m = next(
                        (r for r in case.retrofits if r.saving_fraction is not None and _is_pump_dev(r)),
                        None,
                    )
                if pump_m is None:
                    pump_m = next((r for r in case.retrofits if r.id == 'rf-pump'), None)
                if pump_m is not None:
                    sc.pump_baseline_bound_measure_id = pump_m.id
                    sc.pump_baseline_bound_device_id = pump_m.device_id

    # P0-03：有效节能小时 vs 设备实际运行小时
    if sc.hours is not None and sc.selected_measure_id:
        m_h = next((r for r in case.retrofits if r.id == sc.selected_measure_id), None)
        if m_h and m_h.device_id and m_h.power_drop_kw is not None:
            d_h = next((d for d in case.devices if d.id == m_h.device_id), None)
            if d_h is not None and d_h.hours is not None and float(sc.hours) > float(d_h.hours):
                errors.append(
                    'hours: 预计节能小时超过设备实际运行时间，请重新填写'
                )

    case.scenario = sc
    return errors


@app.middleware('http')
async def session_middleware(request: Request, call_next):
    if request.url.path.startswith('/api'):
        # 健康检查不得创建会话身份或 SQLite 票据。
        if request.url.path == '/api/health' and request.method.upper() in {'GET', 'HEAD'}:
            return await call_next(request)
        cookie_sid = request.cookies.get('ltzs_sid')
        cookie_mac = request.cookies.get('ltzs_stk')
        header_sid = request.headers.get('X-Session-Id')
        token = request.headers.get('X-Project-Token')
        cookie_verified = store.verify_session_mac(cookie_sid, cookie_mac)
        # 只有通过 HMAC 校验的 Cookie 才构成会话身份；Header SID 或任意 Token 都不能取而代之。
        # 未校验的历史 SID 不得占用身份，也不得在本响应中被重新签发信任凭证。
        sid = cookie_sid if cookie_verified else None
        csrf_error = _csrf_guard(request)
        if csrf_error is not None:
            # 仍绑定会话以便响应带上当前合法 Cookie，但拒绝改变状态。
            store.set_request_context(sid, token, cookie_verified=cookie_verified)
            return csrf_error
        sid = store.set_request_context(sid, token, cookie_verified=cookie_verified)
        response = await call_next(request)
        # 以路由执行后的会话为准（例如注销会轮换 SID），避免把已撤销票据再写回 Cookie。
        sid = store.current_session_id() or sid
        current_mac = cookie_mac if (cookie_verified and request.cookies.get('ltzs_sid') == sid) else None
        _attach_session_cookies(response, sid, request, current_mac)
        tok = store.session_project_token()
        if tok:
            response.headers['X-Project-Token'] = tok
        else:
            # 显式空串：前端可清掉旧 Token（如切回公开示范）
            response.headers['X-Project-Token'] = ''
        return response
    return await call_next(request)


@app.get('/api/health')
def health():
    from .version import APP_VERSION, BUILD_ID, SERVICE_NAME
    return {
        'ok': True,
        'service': SERVICE_NAME,
        'version': APP_VERSION,
        'build_id': BUILD_ID,
        'frontend_expected': APP_VERSION,
        'data_persistence': 'local_sqlite',
        'data_persistence_note': (
            '比赛请优先使用本机一键启动；本机数据保存在 backend/data，重启后仍在。'
            '免费云实例（含 SnapDeploy、Render 等）空闲约 15 分钟可能休眠；唤醒可能需要约 60–90 秒，且可能拉起新容器。'
            '同一容器短暂暂停后数据可能还在，但不能保证、也不能承诺休眠/唤醒、重启或重新部署后持续保存企业数据，请勿把免费实例当作长期存储。'
            '请务必导出 TXT/CSV/HTML 备份；正式或比赛演示的企业数据请用本机一键启动。'
            '公开示范写在应用代码中，唤醒后可重新载入；私有企业数据不会写入公开代码仓库。'
            '公网 HTTPS 生产环境请设置 LTZS_COOKIE_SECURE=1，并配置 LTZS_PUBLIC_ORIGIN 为完整的 https 公网地址。'
        ),
    }


@app.get('/api/session')
def session_info():
    scope = store.current_auth_scope()
    return {
        'session_id': store.current_session_id(),
        'active': store.get_active_id(),
        'project_token': store.session_project_token(),
        'cases': store.list_cases(),
        'auth_scope': scope,
        'auth_scope_note': (
            'workspace_session：已验证会话 Cookie，可访问该所有者工作区；'
            '有签名 Cookie 不等于拥有当前项目，必须按项目核对 owner_session。'
            'project_token：仅能访问该 Token 对应的单个项目，访客不等于所有者，不能轮换/撤销凭证或恢复版本；'
            'public_demo：公开示范或尚未授权的访客。'
            '轮换或撤销项目凭证会使所有从旧 Token 衍生的访客授权失效；访客响应不会返回当前有效 Token。'
            '恢复已存在项目的历史版本不会改变原所有者。'
            '退出当前工作区只会撤销会话 Cookie，不会自动作废已发放的项目恢复凭证；如需作废请轮换或撤销该项目凭证。'
        ),
    }


@app.post('/api/session/logout')
def session_logout(request: Request, response: Response):
    """主动注销：撤销当前会话全部票据，签发新的匿名会话。旧 SID+MAC 不可再恢复工作区。

    退出工作区不会自动撤销已发放的项目 Token。
    """
    new_sid = store.revoke_session()
    _attach_session_cookies(response, new_sid, request)
    return {
        'ok': True,
        'session_id': new_sid,
        'active': store.get_active_id(),
        'project_token': None,
        'auth_scope': store.current_auth_scope(),
        'message': '已退出当前工作区，此前的会话 Cookie 已失效。退出不会自动撤销已发放的项目恢复凭证；如需作废请使用「轮换/撤销项目凭证」。',
        'token_revoked': False,
    }


@app.post('/api/session/project-token/rotate')
def session_rotate_project_token():
    """轮换当前项目恢复凭证：旧凭证立即失效，本会话获得新凭证。"""
    try:
        token = store.rotate_project_token()
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {
        'ok': True,
        'project_token': token,
        'session_id': store.current_session_id(),
        'active': store.get_active_id(),
        'message': '已轮换当前项目恢复凭证，此前分享出去的旧凭证已失效。退出工作区不会自动做这次轮换。',
    }


@app.post('/api/session/project-token/revoke')
def session_revoke_project_token():
    """撤销当前项目已发放的恢复凭证。工作区会话仍可访问该项目；仅持有旧凭证的访问会失效。"""
    try:
        store.revoke_project_token()
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {
        'ok': True,
        'project_token': store.session_project_token(),
        'session_id': store.current_session_id(),
        'active': store.get_active_id(),
        'auth_scope': store.current_auth_scope(),
        'message': '已撤销此前发放的项目恢复凭证。若您仍在工作区会话中，可以继续编辑并再次轮换得到新凭证。',
    }


@app.get('/api/cases')
def cases():
    return {
        'cases': store.list_cases(),
        'active': store.get_active_id(),
        'session_id': store.current_session_id(),
        'project_token': store.session_project_token(),
    }


@app.post('/api/cases/empty')
async def create_empty_enterprise(request: Request, name: Optional[str] = None):
    """新建空白企业：不复制金徽示范的设备、措施与历史能源。
    名称同时接受查询参数与 JSON body（与前端 createEmptyEnterprise 一致）。"""
    body_name = None
    try:
        raw = await request.body()
        if raw:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                body_name = payload.get('name')
    except Exception:
        body_name = None
    title = name or body_name or '新企业项目'
    if isinstance(title, str):
        title = title.strip() or '新企业项目'
    else:
        title = '新企业项目'
    case = store.empty_enterprise_case(title)
    return {
        'case': store.case_public_dict(case),
        'results': full_recalculate(),
        'project_token': store.session_project_token(),
        'session_id': store.current_session_id(),
        'message': '已创建空白企业，未复制金徽示范数据。复制示范请使用「恢复公开案例」。',
    }


@app.post('/api/cases/{case_id}/activate')
def activate(case_id: str):
    try:
        c = store.set_active(case_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    return {
        'case': store.case_public_dict(c),
        'results': full_recalculate(),
        'project_token': store.session_project_token(),
        'session_id': store.current_session_id(),
    }


@app.get('/api/case')
def current_case():
    try:
        return store.case_public_dict(store.get_case())
    except PermissionError as e:
        raise HTTPException(403, str(e))


@app.get('/api/results')
def results():
    try:
        return full_recalculate()
    except PermissionError as e:
        raise HTTPException(403, str(e))


@app.get('/api/examples/jinhui')
def example_jinhui():
    """V8 P1-01：只返回示范副本，不切换当前企业。"""
    demo = store._jinhui_case()
    from app.services.compute import recalculate_case
    return recalculate_case(demo)


@app.get('/api/templates')
def templates():
    return {
        'kinds': [
            {'id': 'energy', 'name': '企业能源活动', 'fields': STANDARD_FIELDS['energy']},
            {'id': 'device', 'name': '设备参数', 'fields': STANDARD_FIELDS['device']},
            {'id': 'retrofit', 'name': '技术改造项目', 'fields': STANDARD_FIELDS['retrofit']},
        ]
    }


@app.get('/api/templates/{kind}/download')
def download_template(kind: str):
    try:
        path = template_path(kind)
    except KeyError:
        raise HTTPException(404, '未知模板')
    return FileResponse(path, filename=path.name)


@app.post('/api/upload/preview')
async def upload_preview(kind: str = Query(...), file: UploadFile = File(...)):
    if kind not in ('energy', 'device', 'retrofit'):
        raise HTTPException(400, 'kind 须为 energy|device|retrofit')
    content = await file.read()
    try:
        check_upload_limits(content, file.filename or 'data.csv')
        check_upload_rate(store.current_session_id())
        df = read_table(content, file.filename or 'data.csv')
    except Exception as e:
        raise HTTPException(400, f'无法解析文件: {e}')
    prev = preview_dataframe(df, kind)
    prev['kind'] = kind
    prev['filename'] = file.filename
    return prev


@app.post('/api/upload/commit')
async def upload_commit(
    kind: str = Query(...),
    mappings_json: str = Query('[]'),
    energy_price_policy: str = Query('keep_manual'),
    file: UploadFile = File(...),
):
    if kind not in ('energy', 'device', 'retrofit'):
        raise HTTPException(400, 'kind 无效')
    policy = (energy_price_policy or 'keep_manual').strip()
    if policy not in ('keep_manual', 'use_imported'):
        raise HTTPException(400, 'energy_price_policy 仅支持 keep_manual 或 use_imported')
    try:
        raw = json.loads(mappings_json)
        mappings = [FieldMapping(**m) for m in raw]
    except Exception:
        raise HTTPException(400, 'mappings_json 无效')
    content = await file.read()
    try:
        check_upload_limits(content, file.filename or 'data.csv')
        check_upload_rate(store.current_session_id())
        result = apply_import(
            content, file.filename or 'data.csv', kind, mappings,
            energy_price_policy=policy,
        )
    except Exception as e:
        raise _http_store(e)
    if not result.get('accepted'):
        return JSONResponse(status_code=400, content=result)
    result['results'] = full_recalculate()
    result['session_id'] = store.current_session_id()
    result['project_token'] = store.session_project_token()
    return result


@app.post('/api/devices')
def add_device(body: AddDeviceRequest):
    def mut(case):
        case.devices.append(Device(
            id=f'dev-{uuid.uuid4().hex}',
            name=body.name,
            device_type=body.device_type,
            power_kw=body.power_kw,
            hours=body.hours,
            efficiency=body.efficiency,
            baseline_kwh=body.baseline_kwh,
            output_t=body.output_t,
            data_type=_sanitize_user_data_type(body.data_type, is_demo=case.is_demo),
            source=body.source,
        ))
        case.data_grade = 'device'
    try:
        case = store.update_case(mut)
    except Exception as e:
        raise _http_store(e)
    return {
        'case': store.case_public_dict(case),
        'results': full_recalculate(),
        'project_token': store.session_project_token(),
    }


@app.put('/api/devices/{device_id}')
def update_device(device_id: str, body: UpdateDeviceRequest):
    try:
        orig = store.get_case()
    except PermissionError as e:
        raise HTTPException(403, str(e))
    draft = deepcopy(orig)
    dev = next((d for d in draft.devices if d.id == device_id), None)
    if not dev:
        raise HTTPException(400, f'设备不存在: {device_id}')
    old_type = dev.device_type
    data = body.model_dump(exclude_unset=True)
    # 显式 null 必须有明确语义：名称拒绝；可选数值须带 clear_*，不得 200 空成功还加修订
    if 'name' in data and data['name'] is None:
        raise HTTPException(status_code=422, detail='name 不能为 null')
    clear_map = {
        'power_kw': 'clear_power_kw',
        'hours': 'clear_hours',
        'baseline_kwh': 'clear_baseline_kwh',
        'output_t': 'clear_output_t',
    }
    for field, flag in clear_map.items():
        if field in data and data[field] is None and not data.get(flag):
            raise HTTPException(
                status_code=422,
                detail=f'{field} 为 null 时请使用 {flag}=true 显式清空，不能静默忽略',
            )
    if data.pop('clear_power_kw', None):
        dev.power_kw = None
        data.pop('power_kw', None)
    if data.pop('clear_hours', None):
        dev.hours = None
        data.pop('hours', None)
    if data.pop('clear_baseline_kwh', None):
        dev.baseline_kwh = None
        data.pop('baseline_kwh', None)
    if data.pop('clear_output_t', None):
        dev.output_t = None
        data.pop('output_t', None)
    if 'data_type' in data:
        data['data_type'] = _sanitize_user_data_type(data.get('data_type'), is_demo=draft.is_demo)
    for k, v in data.items():
        if v is None:
            continue
        if hasattr(dev, k):
            setattr(dev, k, v)
    if dev.device_type != old_type:
        on_device_type_changed(draft, dev)
        retire_device_quotes(draft, device_id, '设备类型已更改，旧报价需重新核实适配')
    # 无实际变更则不增加 revision
    orig_dev = next((d for d in orig.devices if d.id == device_id), None)
    if orig_dev is not None and orig_dev.model_dump() == dev.model_dump() and dev.device_type == old_type:
        results = full_recalculate()
        return {'case': store.case_public_dict(orig), 'results': results, 'message': '没有需要保存的变更'}
    try:
        case = store.commit_case(draft)
    except Exception as e:
        raise _http_store(e)
    results = full_recalculate()
    msg = '已保存，节电与减碳结果已更新'
    if dev.device_type != old_type:
        msg = '设备类型已更改。原计算方法不再适用，请重新选择测算方法。' if any(
            (rf.device_id == device_id and not equip_cat.method_compatible_with_device(
                dev, rf.method_code or rf.calculation_method
            ))
            for rf in case.retrofits
        ) else msg
    return {'case': store.case_public_dict(case), 'results': results, 'message': msg}


@app.delete('/api/devices/{device_id}')
def delete_device(device_id: str):
    """软删除：设备及绑定措施移入回收区，active 列表不再参与计算。"""
    def mut(case):
        idx = next((i for i, d in enumerate(case.devices) if d.id == device_id), None)
        if idx is None:
            raise ValueError(f'设备不存在: {device_id}')
        dev = case.devices.pop(idx)
        if not hasattr(case, 'removed_devices') or case.removed_devices is None:
            case.removed_devices = []
        if not hasattr(case, 'removed_retrofits') or case.removed_retrofits is None:
            case.removed_retrofits = []
        # 避免回收区重复堆积同一 id
        case.removed_devices = [d for d in case.removed_devices if d.id != device_id]
        # 标记删除时间（回收站展示用）；恢复时清空
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
        except Exception:
            now = datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')
        dev.removed_at = now
        case.removed_devices.append(dev)
        bound = [rf for rf in case.retrofits if rf.device_id == device_id]
        removed_ids = {rf.id for rf in bound}
        case.retrofits = [rf for rf in case.retrofits if rf.device_id != device_id]
        if bound:
            case.removed_retrofits = [rf for rf in case.removed_retrofits if rf.device_id != device_id]
            case.removed_retrofits.extend(bound)
        if case.scenario.selected_measure_id in removed_ids:
            case.scenario.selected_measure_id = None
            case.scenario.power_drop_kw = None
            case.scenario.investment_cny = None
            case.scenario.investment_bound_measure_id = None
            case.scenario.maintenance_bound_measure_id = None
        # 设备删除：全部关联措施的参考来源失效/归档，保留历史但不保持当前有效
        on_device_deleted(case, device_id, removed_ids)
        # 报价若绑定到该设备/措施则解除并归档
        qc = case.quote_choice
        if qc is not None:
            qm = getattr(qc, 'measure_id', None)
            qd = getattr(qc, 'device_id', None)
            if (qd and qd == device_id) or (qm and qm in removed_ids):
                retire_device_quotes(case, device_id, '设备已删除，旧报价不再作为当前采用')
    try:
        case = store.update_case(mut)
    except Exception as e:
        raise _http_store(e)
    return {'case': store.case_public_dict(case), 'results': full_recalculate()}


@app.get('/api/devices/recycle-bin')
def list_recycle_bin():
    """列出当前评估上下文下已软删除、可恢复的设备（回收站）。"""
    case = store.get_case()
    items = []
    for d in getattr(case, 'removed_devices', None) or []:
        bound = [
            {'id': rf.id, 'name': rf.name}
            for rf in (getattr(case, 'removed_retrofits', None) or [])
            if rf.device_id == d.id
        ]
        items.append({
            'id': d.id,
            'name': d.name,
            'device_type': d.device_type,
            'power_kw': d.power_kw,
            'hours': d.hours,
            'baseline_kwh': d.baseline_kwh,
            'source': d.source,
            'removed_at': getattr(d, 'removed_at', None),
            'bound_retrofits': bound,
        })
    # 新删的在前
    items.sort(key=lambda x: x.get('removed_at') or '', reverse=True)
    return {'items': items, 'count': len(items)}


@app.post('/api/devices/{device_id}/restore')
def restore_device(device_id: str):
    """从回收区恢复设备，并一并恢复该设备下的措施。"""
    def mut(case):
        if not getattr(case, 'removed_devices', None):
            raise ValueError(f'回收区没有该设备: {device_id}')
        if any(d.id == device_id for d in case.devices):
            raise ValueError(f'当前已有同 id 设备，无法恢复: {device_id}')
        idx = next((i for i, d in enumerate(case.removed_devices) if d.id == device_id), None)
        if idx is None:
            raise ValueError(f'回收区没有该设备: {device_id}')
        dev = case.removed_devices.pop(idx)
        dev.removed_at = None
        case.devices.append(dev)
        # 将该 device_id 下 removed_retrofits 一并移回（跳过与现有措施 id 冲突者）
        keep_removed = []
        for rf in list(getattr(case, 'removed_retrofits', None) or []):
            if rf.device_id != device_id:
                keep_removed.append(rf)
                continue
            if any(x.id == rf.id for x in case.retrofits):
                raise ValueError(f'措施 id 冲突，无法恢复: {rf.id}')
            case.retrofits.append(rf)
        case.removed_retrofits = keep_removed
    try:
        case = store.update_case(mut)
    except Exception as e:
        raise _http_store(e)
    return {'case': store.case_public_dict(case), 'results': full_recalculate()}


@app.post('/api/demo/restore-devices')
def restore_demo_devices_alias():
    return restore_demo_default_devices()


@app.post('/api/devices/restore-demo-defaults')
def restore_demo_default_devices():
    """仅金徽示范案例可恢复模板设备；无关企业返回 403。"""
    cur = store.get_case()
    # 只能依据服务端不可伪造的案例归属标识；不得使用可导入的设备 ID、措施 ID 或中文名
    is_public_demo = bool(
        cur.is_demo
        or cur.protected
        or cur.case_id in (store.JINHUI_ID, 'jinhui-demo')
        or getattr(cur, 'origin_case_id', None) in (store.JINHUI_ID, 'jinhui-demo')
    )
    if not is_public_demo:
        raise HTTPException(403, '仅公开示范案例可恢复金徽设备；当前企业不会写入示范记录')
    template = store._jinhui_case()
    tpl_by_id = {d.id: d for d in template.devices if d.id in ('dev-mill', 'dev-pump')}
    tpl_retrofits = {rf.id: rf for rf in template.retrofits if rf.id in ('rf-mill', 'rf-pump')}
    # 名称兜底：硬删后用户可能只记得中文名
    name_to_id = {
        '选厂球磨机': 'dev-mill',
        '二级输送泵': 'dev-pump',
    }
    restored_from_bin: list[str] = []
    added_from_tpl: list[str] = []

    def mut(case):
        if not getattr(case, 'removed_devices', None):
            case.removed_devices = []
        if not getattr(case, 'removed_retrofits', None):
            case.removed_retrofits = []

        def has_demo_device(did: str, name: str) -> bool:
            return any(d.id == did or (d.name or '') == name for d in case.devices)

        for did, tpl_dev in tpl_by_id.items():
            name = tpl_dev.name
            if has_demo_device(did, name):
                continue
            # 1) 回收区按 id
            idx = next((i for i, d in enumerate(case.removed_devices) if d.id == did), None)
            # 2) 回收区按名称
            if idx is None:
                idx = next((i for i, d in enumerate(case.removed_devices) if (d.name or '') == name), None)
            if idx is not None:
                dev = case.removed_devices.pop(idx)
                dev.removed_at = None
                # 若 id 与现有冲突，换新 id 并重绑措施
                if any(d.id == dev.id for d in case.devices):
                    old_id = dev.id
                    dev.id = f'dev-{uuid.uuid4().hex}'
                    for rf in list(case.removed_retrofits):
                        if rf.device_id == old_id:
                            rf.device_id = dev.id
                case.devices.append(dev)
                keep = []
                for rf in case.removed_retrofits:
                    if rf.device_id != dev.id:
                        keep.append(rf)
                        continue
                    if any(x.id == rf.id for x in case.retrofits):
                        rf = deepcopy(rf)
                        rf.id = f'rf-{uuid.uuid4().hex}'
                    case.retrofits.append(rf)
                case.removed_retrofits = keep
                restored_from_bin.append(did)
                continue
            # 3) 模板深拷贝（硬删场景）
            fresh = deepcopy(tpl_dev)
            if any(d.id == fresh.id for d in case.devices):
                fresh.id = f'dev-{uuid.uuid4().hex}'
            case.devices.append(fresh)
            added_from_tpl.append(did)
            # 对应措施
            for rid, tpl_rf in tpl_retrofits.items():
                if tpl_rf.device_id != did and tpl_rf.target_device != name:
                    # 模板里 device_id 对应该 did
                    if tpl_by_id.get(tpl_rf.device_id) and tpl_by_id[tpl_rf.device_id].name != name:
                        continue
                if tpl_rf.device_id != did:
                    continue
                if any(rf.id == rid for rf in case.retrofits):
                    continue
                if any((rf.name == tpl_rf.name and rf.device_id == fresh.id) for rf in case.retrofits):
                    continue
                rf = deepcopy(tpl_rf)
                rf.device_id = fresh.id
                if any(x.id == rf.id for x in case.retrofits):
                    rf.id = f'rf-{uuid.uuid4().hex}'
                case.retrofits.append(rf)

        # 再扫一遍：设备已在、措施缺失时补措施
        active_by_name = {(d.name or ''): d for d in case.devices}
        for rid, tpl_rf in tpl_retrofits.items():
            if any(rf.id == rid for rf in case.retrofits):
                continue
            # 找目标设备
            target = next((d for d in case.devices if d.id == tpl_rf.device_id), None)
            if target is None:
                target = active_by_name.get(tpl_rf.target_device or '')
            if target is None:
                continue
            idx = next((i for i, rf in enumerate(case.removed_retrofits) if rf.id == rid or (rf.name == tpl_rf.name and rf.device_id in (tpl_rf.device_id, target.id))), None)
            if idx is not None:
                rf = case.removed_retrofits.pop(idx)
                rf.device_id = target.id
                if any(x.id == rf.id for x in case.retrofits):
                    rf.id = f'rf-{uuid.uuid4().hex}'
                case.retrofits.append(rf)
            else:
                rf = deepcopy(tpl_rf)
                rf.device_id = target.id
                if any(x.id == rf.id for x in case.retrofits):
                    rf.id = f'rf-{uuid.uuid4().hex}'
                case.retrofits.append(rf)

    try:
        case = store.update_case(mut)
    except Exception as e:
        raise _http_store(e)
    id_to_name = {'dev-mill': '选厂球磨机', 'dev-pump': '二级输送泵'}
    names = [id_to_name.get(x, x) for x in (restored_from_bin + added_from_tpl)]
    msg = ('已恢复：' + '、'.join(names)) if names else '所需设备已在列表中，无需恢复'
    return {
        'case': store.case_public_dict(case),
        'results': full_recalculate(),
        'restored_from_bin': list(restored_from_bin),
        'added_from_template': list(added_from_tpl),
        'message': msg,
    }


@app.post('/api/retrofits')
def add_retrofit(body: AddRetrofitRequest):
    def mut(case):
        if not any(d.id == body.device_id for d in case.devices):
            raise ValueError(f'目标设备不存在: {body.device_id}')
        from .models.schema import PROFESSIONAL_METHOD_CODES as _PRO
        from .models.schema import ReferenceSelection  # noqa: F401 — keep import local-safe
        dev = next(d for d in case.devices if d.id == body.device_id)
        drop = body.power_drop_kw
        frac = body.saving_fraction
        method = body.method_code or body.calculation_method
        model_inputs = dict(body.model_inputs or {}) if body.model_inputs else None
        if drop is not None and frac is not None and method is None:
            raise ValueError('同时填写功率降幅与节电比例时，必须指定 calculation_method')
        # 解析设备类型并校验方法白名单
        type_code = equip_cat.resolve_type_code(dev.device_type, dev.name)
        if method is None:
            # 兼容旧客户端：仅填功率差或比例
            if drop is not None and frac is None:
                method = 'power_drop_hours'
            elif frac is not None and drop is None:
                method = 'baseline_fraction'
        if method is not None and not equip_cat.method_allowed_for_type(type_code, method):
            raise ValueError(
                f'设备类型「{equip_cat.get_type(type_code) and equip_cat.get_type(type_code).get("display_name") or type_code}」'
                f'不支持该方法；可用：{"／".join(equip_cat.available_methods_zh(type_code).split("／"))}'
            )
        if method in _PRO:
            # 专业方法：必须带工况；创建前先跑同一引擎，拒绝无效输入
            if not model_inputs:
                raise ValueError(f'专业方法 {method} 需要提供 model_inputs 工况数据')
            trial = phys.run_method(method, model_inputs)
            if trial.get('status') != 'ok':
                raise ValueError(trial.get('error') or '专业模型输入无效')
            drop = None
            frac = None
        elif method == 'power_drop_hours':
            frac = None
            model_inputs = None
        elif method == 'baseline_fraction':
            drop = None
            model_inputs = None
        if drop is not None and dev.power_kw is not None and float(drop) > float(dev.power_kw):
            raise ValueError(
                f'功率降幅 {drop} kW 大于设备功率 {dev.power_kw} kW，'
                f'物理上不成立；请区分额定功率与改造前平均运行功率'
            )
        rid = f'rf-{uuid.uuid4().hex}'
        rf = RetrofitProject(
            id=rid,
            name=body.name,
            target_device=body.target_device or dev.name,
            device_id=body.device_id,
            power_drop_kw=drop,
            saving_fraction=frac,
            calculation_method=method,
            method_code=method,
            model_inputs=model_inputs,
            investment_cny=body.investment_cny,
            annual_maintenance_cny=body.annual_maintenance_cny,
            data_type=_sanitize_user_data_type(body.data_type, is_demo=case.is_demo),
            source=body.source,
            note=body.note,
        )
        case.retrofits.append(rf)
        if body.select:
            case.scenario.selected_measure_id = rid
            if rf.power_drop_kw is not None:
                case.scenario.power_drop_kw = rf.power_drop_kw
            else:
                case.scenario.power_drop_kw = None
            if rf.investment_cny is not None:
                case.scenario.investment_cny = rf.investment_cny
                case.scenario.investment_bound_measure_id = rid
            else:
                case.scenario.investment_cny = None
                case.scenario.investment_bound_measure_id = None
            case.scenario.annual_maintenance_cny = rf.annual_maintenance_cny
            case.scenario.maintenance_bound_measure_id = rid
            # P0-11：切换措施后解除不匹配报价
            qc = case.quote_choice
            if qc is not None:
                qm = getattr(qc, 'measure_id', None)
                qd = getattr(qc, 'device_id', None)
                if (qm and qm != rid) or (qd and qd != body.device_id):
                    case.quote_choice = None
    try:
        case = store.update_case(mut)
    except Exception as e:
        raise _http_store(e)
    return {'case': store.case_public_dict(case), 'results': full_recalculate()}


def _snapshot_retrofit(rf) -> dict:
    return rf.model_dump() if hasattr(rf, 'model_dump') else dict(rf)


@app.put('/api/retrofits/{rid}')
def update_retrofit(rid: str, body: dict):
    """编辑已有措施：复制整案→统一校验→试算→原子提交。"""
    data = dict(body or {})
    draft_only = bool(data.get('save_as_draft') or data.get('draft_only'))
    try:
        orig = store.get_case()
    except PermissionError as e:
        raise HTTPException(403, str(e))
    draft = deepcopy(orig)
    try:
        validate_and_apply_retrofit_update(draft, rid, data, draft_only=draft_only)
    except ChangeRejected as e:
        raise HTTPException(status_code=e.status, detail={'message': e.message, 'errors': e.errors})
    except Exception as e:
        raise HTTPException(400, detail={'message': str(e), 'errors': [str(e)]})
    try:
        case = store.commit_case(draft)
    except Exception as e:
        raise _http_store(e)
    message = '已保存为草稿，不参与推荐和正式报告。' if draft_only else '已保存，节电与减碳结果已更新'
    return {'case': store.case_public_dict(case), 'results': full_recalculate(), 'message': message}


@app.delete('/api/retrofits/{rid}')
def delete_retrofit(rid: str):
    try:
        orig = store.get_case()
    except PermissionError as e:
        raise HTTPException(403, str(e))
    draft = deepcopy(orig)
    rf = next((x for x in draft.retrofits if x.id == rid), None)
    if rf is None:
        raise HTTPException(400, f'措施不存在: {rid}')
    on_measure_deleted(draft, rid)
    draft.retrofits = [x for x in draft.retrofits if x.id != rid]
    draft.removed_retrofits = list(draft.removed_retrofits or []) + [rf]
    if draft.scenario.selected_measure_id == rid:
        draft.scenario.selected_measure_id = draft.retrofits[0].id if draft.retrofits else None
    try:
        case = store.commit_case(draft)
    except Exception as e:
        raise _http_store(e)
    return {'case': store.case_public_dict(case), 'results': full_recalculate(), 'message': '已保存，节电与减碳结果已更新'}


@app.post('/api/retrofits/{rid}/copy')
def copy_retrofit(rid: str):
    try:
        orig = store.get_case()
    except PermissionError as e:
        raise HTTPException(403, str(e))
    draft = deepcopy(orig)
    rf = next((x for x in draft.retrofits if x.id == rid), None)
    if rf is None:
        raise HTTPException(400, f'措施不存在: {rid}')
    data = prepare_copied_measure(rf, f'rf-{uuid.uuid4().hex}')
    cloned = RetrofitProject(**{k: v for k, v in data.items() if k in RetrofitProject.model_fields})
    draft.retrofits.append(cloned)
    draft.scenario.selected_measure_id = cloned.id
    add_copied_provenance(draft, cloned, rf)
    try:
        case = store.commit_case(draft)
    except Exception as e:
        raise _http_store(e)
    return {
        'case': store.case_public_dict(case),
        'results': full_recalculate(),
        'message': '已复制参数。请核对新设备是否适用，确认前只作参考试算。',
    }


@app.post('/api/retrofits/{rid}/revert')
def revert_retrofit(rid: str):
    try:
        orig = store.get_case()
    except PermissionError as e:
        raise HTTPException(403, str(e))
    draft = deepcopy(orig)
    rf = next((x for x in draft.retrofits if x.id == rid), None)
    if rf is None:
        raise HTTPException(400, f'措施不存在: {rid}')
    prev = getattr(rf, 'previous_snapshot', None)
    if not prev:
        raise HTTPException(400, '没有可恢复的上一版本')
    keep_id = rf.id
    prev_data = dict(prev)
    extra = {'_refs': prev_data.pop('_refs', None), '_pvs': prev_data.pop('_pvs', None)}
    current_snap = {**_snapshot_retrofit(rf), **snapshot_measure_refs(draft, keep_id)}
    restored = RetrofitProject(**{k: v for k, v in {**prev_data, 'id': keep_id}.items() if k in RetrofitProject.model_fields})
    restored.previous_snapshot = current_snap
    idx = draft.retrofits.index(rf)
    draft.retrofits[idx] = restored
    restore_measure_refs(draft, keep_id, extra if extra.get('_refs') is not None else None)
    draft.scenario.selected_measure_id = keep_id
    try:
        case = store.commit_case(draft)
    except Exception as e:
        raise _http_store(e)
    return {'case': store.case_public_dict(case), 'results': full_recalculate(), 'message': '已保存，节电与减碳结果已更新'}


@app.put('/api/scenario')
def update_scenario(body: UpdateScenarioRequest):
    """先复制→校验→试算→原子提交；失败回滚。"""
    try:
        draft = store.get_case()
    except PermissionError as e:
        raise HTTPException(403, str(e))

    # 示范只读：在副本上改，通过后再 commit（fork）
    draft = deepcopy(draft)
    field_errors = _apply_scenario_to_case(draft, body)
    if field_errors:
        raise HTTPException(status_code=400, detail={'errors': field_errors, 'message': 'invalid_input'})

    # 试算（先全链计算再原子提交，失败不写库）
    try:
        trial = recalculate_case(draft)
        _assert_results_finite(trial)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f'试算失败: {e}')

    mill = trial.get('mill_scenario') or {}
    if mill.get('status') == 'invalid_input':
        raise HTTPException(
            status_code=400,
            detail={'errors': [mill.get('error') or 'invalid_input'], 'message': mill.get('error')},
        )
    pump = trial.get('pump_scenario') or {}
    if pump.get('status') == 'invalid_input':
        raise HTTPException(
            status_code=400,
            detail={'errors': [pump.get('error') or 'invalid_input'], 'message': pump.get('error')},
        )

    # 通过后提交
    try:
        case = store.commit_case(draft)
    except Exception as e:
        raise _http_store(e)

    results = recalculate_case(case)
    _assert_results_finite(results)
    return {
        'case': store.case_public_dict(case),
        'results': results,
        'project_token': store.session_project_token(),
        'session_id': store.current_session_id(),
    }


@app.post('/api/scenarios/calculate')
def calculate_scenario(body: UpdateScenarioRequest):
    return update_scenario(body)


@app.post('/api/schemes/compare')
def schemes_compare(body: CompareSchemesRequest):
    try:
        case = store.get_case()
        return compare_measures(case, body.measure_a_id, body.measure_b_id, body.budget_cny)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise _http_store(e)
    except PermissionError as e:
        raise HTTPException(403, str(e))


@app.post('/api/custom-params')
def add_custom_param(body: CustomParamRequest):
    """V8 P0-02：映射字段与 PUT /api/scenario 同一校验入口，失败整单回滚。"""
    original = store.get_case()
    draft = deepcopy(original)
    draft.custom_params.append(CustomParam(**body.model_dump()))
    mapping = body.mapped_model_field
    allowed = {
        'power_drop_kw', 'hours', 'energy_price', 'emission_factor',
        'investment_cny', 'annual_maintenance_cny', 'efficiency', 'output_t',
        'baseline_power_kw', 'pump_baseline_kwh',
    }
    if mapping:
        if mapping not in allowed:
            raise HTTPException(400, f'未验证的模型字段，拒绝参与计算: {mapping}')
        try:
            sc_data = draft.scenario.model_dump()
            sc_data[mapping] = body.value
            if mapping == 'emission_factor':
                sc_data['emission_factor_user_override'] = True
            if mapping == 'energy_price':
                sc_data['energy_price_user_override'] = True
            draft.scenario = ScenarioParams.model_validate(sc_data)
            # 整案再校验
            CaseState.model_validate(draft.model_dump())
        except Exception as e:
            raise HTTPException(400, f'自定义参数校验失败（已回滚）: {e}')
    trial = recalculate_case(draft)
    mill = trial.get('mill_scenario') or {}
    sa = trial.get('selected_assessment') or {}
    if mill.get('status') == 'invalid_input':
        raise HTTPException(400, mill.get('error') or 'invalid_input')
    if sa.get('status') == 'invalid_input':
        raise HTTPException(400, sa.get('error') or sa.get('waiting_message') or 'invalid_input')
    case = store.commit_case(draft)
    return {'case': store.case_public_dict(case), 'results': recalculate_case(case)}


@app.post('/api/versions/save')
def save_version(label: Optional[str] = None):
    try:
        return store.save_version(label)
    except PermissionError as e:
        raise HTTPException(403, str(e))


@app.get('/api/versions')
def versions():
    return {'versions': store.list_versions()}


@app.post('/api/versions/{version_id}/restore')
def restore(version_id: str):
    try:
        case = store.restore_version(version_id)
    except FileNotFoundError:
        raise HTTPException(404, '版本不存在')
    except PermissionError as e:
        raise HTTPException(403, str(e))
    return {'case': store.case_public_dict(case), 'results': full_recalculate()}


@app.get('/api/versions/compare')
def compare(a: str, b: str):
    try:
        return store.compare_versions(a, b)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))


@app.get('/api/export/report')
def export_report():
    try:
        text = build_report_text()
    except PermissionError as e:
        raise HTTPException(403, str(e))
    return PlainTextResponse(text, media_type='text/plain; charset=utf-8')


@app.get('/api/export/csv')
def export_csv():
    """P0-04/08：csv.writer 统一转义；与 selected_assessment 同一事实源。"""
    try:
        data = full_recalculate()
    except PermissionError as e:
        raise HTTPException(403, str(e))
    from app.services.compute import render_csv_rows
    sa = data.get('selected_assessment') or {}
    sc = data['case']['scenario']
    ba = data.get('before_after') or {}
    fm = data.get('factor_meta') or {}
    snap = data.get('assessment_snapshot') or {}
    rows = [
        ['字段', '数值', '单位', '标记'],
        ['案例', data['case']['name'], '', ''],
        ['设备', sa.get('device_name') or '', '', ''],
        ['措施', sa.get('measure_name') or '', '', ''],
        ['措施ID', sa.get('measure_id') or '', '', ''],
        ['计算方法', sa.get('calculation_method') or '', '', ''],
        ['模型', sa.get('model_label') or sa.get('technology_type') or '', '', ''],
        ['有效小时', sc.get('hours'), 'h', '假设'],
        ['功率降低', sc.get('power_drop_kw') if sa.get('calculation_method') == 'power_drop_hours' else '', 'kW', '披露' if data['case'].get('is_demo') else '假设'],
        ['情景节电', sa.get('saved_kwh', ''), 'kWh', '模型估算'],
        ['改造前', ba.get('before_kwh') if ba.get('before_kwh') is not None else ba.get('display_before') or '', 'kWh', ''],
        ['改造后', ba.get('after_kwh') if ba.get('after_kwh') is not None else ba.get('display_after') or '', 'kWh', ''],
        ['情景减排', sa.get('co2_reduction_t', ''), 'tCO2', '模型估算'],
        ['因子说明', fm.get('label') or fm.get('source_name') or '', '', ''],
        ['情景电费节省', sa.get('annual_cost_saving_cny') if sa.get('annual_cost_saving_cny') is not None else (sa.get('cost_note') or ''), '元', '模型估算'],
        ['回收期', sa.get('simple_payback_years') if sa.get('simple_payback_years') is not None else sa.get('payback_message', ''), '年', ''],
        ['snapshot_id', snap.get('snapshot_id') or '', '', ''],
    ]
    return PlainTextResponse(render_csv_rows(rows), media_type='text/csv; charset=utf-8')




@app.get('/api/equipment/types')
def equipment_types():
    return {
        'types': equip_cat.list_types(),
        'display_options': equip_cat.display_options(),
        'capability_matrix': equip_cat.capability_matrix(),
    }


@app.get('/api/equipment/types/{type_code}/methods')
def equipment_type_methods(type_code: str):
    t = equip_cat.get_type(type_code)
    if not t:
        raise HTTPException(404, '未知设备类型')
    return {'type': t, 'methods': equip_cat.methods_for(type_code)}


@app.get('/api/equipment/resolve')
def equipment_resolve(device_type: str = '', name: str = ''):
    """旧「泵」类型确认：不静默改成 generic，并返回可用方法以免方法栏空白。"""
    return equip_cat.resolve_type_detail(device_type, name)


@app.post('/api/equipment/assessments/calculate')
def equipment_assess_calculate(body: dict):
    """物理/效率模型试算（不持久化）。无效参数→400，永不500。与创建措施共用同一引擎。"""
    raw_method = (body or {}).get('method_code')
    if raw_method is not None and not isinstance(raw_method, str):
        raise HTTPException(status_code=422, detail='method_code 必须是字符串')
    method = raw_method or ''
    payload = dict(body or {})
    device_hours = payload.pop('device_hours', None)
    result = phys.run_method(method, payload)
    if result.get('status') == 'invalid_input':
        raise HTTPException(
            status_code=400,
            detail={'errors': [result.get('error') or 'invalid_input'], 'message': result.get('error') or 'invalid_input'},
        )
    if result.get('status') != 'ok':
        raise HTTPException(
            status_code=400,
            detail={'errors': [result.get('error') or f'暂不支持或未知 method_code: {method}'], 'message': 'invalid_input'},
        )
    saved = result.get('saved_kwh')
    if saved is not None and (not isinstance(saved, (int, float)) or not math.isfinite(float(saved))):
        raise HTTPException(status_code=400, detail={'errors': ['参数超出合理范围，请检查单位'], 'message': 'invalid_input'})
    # 试算也可附带设备小时做冲突提示（不入库）
    try:
        mi_hours = (payload.get('hours') if isinstance(payload, dict) else None)
        if device_hours is not None and mi_hours is not None and float(mi_hours) > float(device_hours):
            from .services.compute import _hours_conflict_message
            result = dict(result)
            result['hours_conflict'] = True
            result['hours_conflict_message'] = _hours_conflict_message(float(mi_hours), float(device_hours))
            result['recommendable'] = False
    except (TypeError, ValueError):
        pass
    return result


@app.get('/api/references')
def list_references(type_code: str | None = None, method_code: str | None = None):
    import json
    from pathlib import Path as _P
    path = _P(__file__).resolve().parents[1] / 'data' / 'reference' / 'reference_registry.json'
    if not path.is_file():
        raise HTTPException(status_code=503, detail='参考资料登记表暂不可用')
    data = json.loads(path.read_text(encoding='utf-8'))
    sources = list(data.get('sources') or [])

    def _type_ok(s, tc: str) -> bool:
        at = s.get('applicable_type_codes')
        if at is not None:
            return tc in at
        # 兼容旧 model_scope：展开设备→其方法
        scope = s.get('model_scope') or []
        if tc in scope:
            return True
        # 设备的支持方法任一命中 scope 也算
        methods = (equip_cat.get_type(tc) or {}).get('supported_methods') or []
        return any(m in scope for m in methods)

    def _method_ok(s, mc: str) -> bool:
        am = s.get('applicable_method_codes')
        if am is not None:
            return mc in am
        scope = s.get('model_scope') or []
        return mc in scope or mc.split('_')[0] in str(scope)

    if type_code:
        sources = [s for s in sources if _type_ok(s, type_code)]
    if method_code:
        sources = [s for s in sources if _method_ok(s, method_code)]
    return {
        'version': data.get('version'),
        'last_checked_at': data.get('last_checked_at'),
        'sources': sources,
        'offline': True,
        'note': '展示上次人工核验快照；不会对任意 URL 发起抓取',
    }


@app.post('/api/references/fetch')
def references_fetch(body: dict):
    """防 SSRF：拒绝非白名单与私网地址。"""
    import json
    from pathlib import Path as _P
    from urllib.parse import urlparse
    url = (body or {}).get('url') or ''
    path = _P(__file__).resolve().parents[1] / 'data' / 'reference' / 'reference_registry.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    policy = data.get('fetch_policy') or {}
    hosts = set(policy.get('whitelist_hosts') or [])
    try:
        u = urlparse(url)
    except Exception:
        raise HTTPException(400, 'URL 无效')
    host = (u.hostname or '').lower()
    if host in ('127.0.0.1', 'localhost', '0.0.0.0', '::1') or host.startswith('192.168.') or host.startswith('10.') or host.endswith('.local'):
        raise HTTPException(400, '拒绝访问本机/私网地址（防 SSRF）')
    if host not in hosts:
        raise HTTPException(400, f'域名不在白名单，拒绝检索: {host or "(空)"}')
    # 比赛版：不实际联网抓取，返回缓存说明
    return {
        'ok': False,
        'cached': True,
        'message': f'暂时无法查询最新资料，以下展示的是上次核实的公开记录（日期：{data.get("last_checked_at")}）。您仍可以上传自己的报价继续测算。',
        'sources': data.get('sources') or [],
    }



@app.post('/api/references/select')
def references_select(body: dict):
    """互斥动作：收藏 / 不使用 / 用自己的数据 / 确认用于试算。reject 与 use_own 永不写入计算参数。"""
    import json
    from datetime import datetime, timezone, timedelta
    from pathlib import Path as _P
    from .services.reference_apply import apply_reference_selection

    source_id = (body or {}).get('source_id')
    if source_id is not None and not isinstance(source_id, str):
        raise HTTPException(status_code=422, detail='source_id 必须是字符串')
    path = _P(__file__).resolve().parents[1] / 'data' / 'reference' / 'reference_registry.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    sources = {s.get('source_id'): s for s in (data.get('sources') or [])}
    src = sources.get(source_id) if source_id else None
    if source_id and src is None:
        raise HTTPException(404, f'未知 source_id: {source_id}')

    tz = timezone(timedelta(hours=8))
    selected_at = datetime.now(tz).isoformat(timespec='seconds')
    try:
        return apply_reference_selection(
            body or {}, src=src, registry_version=data.get('version'), selected_at=selected_at,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise _http_store(e)


@app.get('/api/procurement/catalog')
def procurement_catalog(equipment_category: Optional[str] = None, project_id: Optional[str] = None):
    """公开采购目录 + 本项目私有询价（仅当前项目可见）。

    公开候选报价必须始终可列：项目会话权限异常时仍返回公开目录，
    避免前端「候选报价企业」下拉空白。
    """
    case = None
    user_quotes: list = []
    active_choice = None
    access_note = None
    try:
        case = store.get_case()
        user_quotes = list(getattr(case, 'user_quotes', None) or [])
        raw_choice = case.quote_choice.model_dump() if case.quote_choice else None
        if raw_choice and (raw_choice.get('superseded') or raw_choice.get('active') is False):
            raw_choice['current'] = False
            raw_choice['status'] = 'historical'
            active_choice = None
        else:
            active_choice = raw_choice

    except PermissionError:
        access_note = '当前会话暂时读不到本项目私有询价，仍可查看公开采购候选报价。'
    except KeyError:
        access_note = '当前项目未找到；仍可查看公开采购候选报价。'
    except Exception:
        access_note = '项目状态异常；仍可查看公开采购候选报价。'

    payload = proc_cat.catalog_payload(user_quotes=user_quotes)
    # 供应商候选下拉默认不含「金徽参考」（那不是供应商采购报价）
    public_supplier_quotes = [
        q for q in payload['quotes']
        if q.get('quote_kind') != 'jinhui_reference'
    ]
    payload['quotes'] = public_supplier_quotes
    payload['reference_params'] = [
        q for q in proc_cat.list_quotes()
        if q.get('quote_kind') == 'jinhui_reference'
    ]
    payload['projects'] = [
        p for p in payload['projects']
        if p.get('equipment_category') != '参考'
    ]
    if equipment_category or project_id:
        payload['projects'] = [
            p for p in proc_cat.list_projects(equipment_category)
            if p.get('equipment_category') != '参考'
        ]
        payload['quotes'] = [
            q for q in proc_cat.list_quotes(
                project_id=project_id,
                equipment_category=equipment_category,
                user_quotes=user_quotes,
            )
            if q.get('quote_kind') != 'jinhui_reference'
        ]
    payload['active_quote_choice'] = active_choice
    if access_note:
        payload['access_note'] = access_note
    payload['quote_count'] = len(payload['quotes'])
    return payload


@app.post('/api/procurement/user-quotes')
def add_user_quote(body: UserQuoteRequest):
    """添加本项目私有询价（不进入公开目录）。"""
    try:
        draft = deepcopy(store.get_case())
    except PermissionError as e:
        raise HTTPException(403, str(e))
    if draft.is_demo or draft.protected:
        raise HTTPException(400, '公开示范只读：请先进入企业项目再添加私有询价')
    qid = f"uq-{uuid.uuid4().hex[:10]}"
    item = {
        'quote_id': qid,
        'project_id': f'user-{draft.case_id}',
        'bidder_full_name': body.label,
        'label': body.label,
        'project_title': body.project_title,
        'total_quote_cny': body.total_quote_cny,
        'quote_kind': 'user_quote',
        'equipment_category': body.device_type,
        'source_url': body.source_url or '',
        'notes': body.note or '企业自行提供',
        'procurement_status': 'user_private',
        'currency': 'CNY',
        'rank': None,
        'spec_verified': False,
        'scope_verified': False,
    }
    draft.user_quotes = list(draft.user_quotes or []) + [item]
    case = store.commit_case(draft)
    return {'ok': True, 'quote': item, 'case': store.case_public_dict(case)}


@app.post('/api/procurement/apply-quote')
def apply_quote(body: ApplyQuoteRequest):
    """确认参考报价。采购候选价默认只做价格敏感性，不得写成完整技改投资确定回收期。"""
    try:
        draft = deepcopy(store.get_case())
    except PermissionError as e:
        raise HTTPException(403, str(e))

    before = recalculate_case(draft)
    before_sa = before.get('selected_assessment') or {}
    before_kwh = before_sa.get('saved_kwh')
    before_co2 = before_sa.get('co2_reduction_t')

    quote = proc_cat.get_quote(body.quote_id, user_quotes=list(draft.user_quotes or []))
    if not quote:
        raise HTTPException(404, '报价不存在或不属于本项目')

    measure = None
    mid = body.measure_id or draft.scenario.selected_measure_id
    if mid:
        measure = next((r for r in draft.retrofits if r.id == mid), None)
    device_type = None
    measure_tech = None
    if measure is not None:
        if measure.device_id:
            d = next((x for x in draft.devices if x.id == measure.device_id), None)
            device_type = d.device_type if d else measure.target_device
        else:
            device_type = measure.target_device
        # 粗分技术类型
        if measure.saving_fraction is not None and measure.power_drop_kw is None:
            measure_tech = 'pump'
        elif '变频' in (measure.name or ''):
            measure_tech = 'vfd'
        else:
            measure_tech = 'process'

    compat = proc_cat.assess_compatibility(
        quote,
        device_type=device_type,
        measure_tech=measure_tech,
        confirm_same_scope=(body.compatibility_result == 'compatible'),
        confirm_spec_and_scope=bool(getattr(body, 'confirm_spec_and_scope', False)),
    )
    result = body.compatibility_result or compat['compatibility_result']
    if result == 'incompatible' or compat.get('compatibility_result') == 'incompatible':
        raise HTTPException(400, compat.get('message') or '不适配，只能查看对照')
    if not body.confirm_reference_scenario:
        raise HTTPException(400, '须确认「按此参考报价试算」')

    # 金徽电价参考：走电价通道，不进供应商投资
    if quote.get('reference_field') == 'energy_price' and quote.get('reference_value') is not None:
        draft.scenario.use_jinhui_tariff_reference = True
        draft.scenario.energy_price = float(quote['reference_value'])
        draft.scenario.energy_price_user_override = False
        choice = QuoteChoice(
            quote_id=quote['quote_id'],
            project_id=quote.get('project_id'),
            bidder_full_name=quote.get('bidder_full_name'),
            label=quote.get('label'),
            project_title='金徽参考电价（非供应商采购）',
            total_quote_cny=None,
            quote_kind=quote.get('quote_kind'),
            quote_usage_mode='tariff_reference',
            compatibility_result='compatible',
            watermark='参考电价、并非本企业实际电价',
            source_url=quote.get('source_url') or '',
            applied_to_investment=False,
            compatibility_verified_by_user=False,
            is_full_retrofit_investment=False,
        )
        draft.quote_choice = choice
        case = store.commit_case(draft)
        after = recalculate_case(case)
        after_sa = after.get('selected_assessment') or {}
        return {
            'ok': True,
            'quote_choice': choice.model_dump(),
            'compatibility': compat,
            'energy_unchanged': before_kwh == after_sa.get('saved_kwh'),
            'carbon_unchanged': before_co2 == after_sa.get('co2_reduction_t'),
            'case': store.case_public_dict(case),
            'results': after,
            'watermark': choice.watermark,
        }

    proj = proc_cat.get_project(quote.get('project_id') or '') or {}
    as_sensitivity = bool(getattr(body, 'as_price_sensitivity', False)) or bool(body.apply_to_investment)
    # P0-03：公开候选价 / 类别不符 → 强制价格敏感性，绝不写入完整技改投资
    force_sensitivity = (
        quote.get('quote_kind') == 'public_bid'
        or compat.get('can_apply_investment') is False
        or measure_tech == 'process'
        or not getattr(body, 'confirm_spec_and_scope', False)
    )
    if body.apply_to_investment and force_sensitivity:
        as_sensitivity = True

    usage = 'price_sensitivity' if as_sensitivity else 'explicit_reference_scenario'
    choice = QuoteChoice(
        quote_id=quote['quote_id'],
        project_id=quote.get('project_id'),
        bidder_full_name=quote.get('bidder_full_name'),
        label=quote.get('label'),
        project_title=proj.get('project_title') or quote.get('project_title'),
        total_quote_cny=quote.get('total_quote_cny'),
        quote_kind=quote.get('quote_kind'),
        quote_usage_mode=usage,
        compatibility_result='needs_adaptation' if as_sensitivity else result,
        watermark=(
            '价格敏感性假设实验：非完整技改投资，不可作为确定回收期'
            if as_sensitivity else '参考报价情景'
        ),
        source_url=quote.get('source_url') or '',
        applied_to_investment=False,
        compatibility_verified_by_user=bool(getattr(body, 'confirm_spec_and_scope', False)),
        sensitivity_investment_cny=(float(quote['total_quote_cny']) if quote.get('total_quote_cny') is not None and as_sensitivity else None),
        is_full_retrofit_investment=False,
        measure_id=measure.id if measure is not None else mid,
        device_id=measure.device_id if measure is not None else None,
    )

    # 绝不把变频器历史候选价写入 scenario.investment_cny 作为完整投资
    # 仅当用户明确确认规格范围且非 process 错配、且非 public_bid 强制时才允许——当前策略：一律不自动写入
    draft.quote_choice = choice
    case = store.commit_case(draft)
    after = recalculate_case(case)
    after_sa = after.get('selected_assessment') or {}
    return {
        'ok': True,
        'quote_choice': choice.model_dump(),
        'compatibility': compat,
        'energy_unchanged': before_kwh == after_sa.get('saved_kwh'),
        'carbon_unchanged': before_co2 == after_sa.get('co2_reduction_t'),
        'investment_unchanged': True,
        'price_sensitivity': as_sensitivity,
        'case': store.case_public_dict(case),
        'results': after,
        'watermark': choice.watermark,
        'message': choice.watermark,
    }



def _guard_report_request(body: ReportExportRequest):
    fmt = (body.format or 'txt').lower()
    if fmt not in ('txt', 'csv', 'html'):
        if fmt == 'pdf':
            raise HTTPException(
                400,
                '尚未提供原生 PDF 引擎。请导出 HTML 后用浏览器打印为 PDF，或使用 TXT/CSV。',
            )
        raise HTTPException(400, f'不支持的报告格式: {fmt}（仅支持 txt|csv|html）')
    preset = (body.preset or 'full').strip().lower()
    if preset == 'custom' and body.sections is not None and len(body.sections) == 0:
        raise HTTPException(400, '请至少选择一个报告章节，不能导出空报告')
    return fmt


@app.post('/api/reports/preview')
def reports_preview(body: ReportExportRequest):
    _guard_report_request(body)
    try:
        payload = full_recalculate()
    except PermissionError as e:
        raise HTTPException(403, str(e))
    built = report_sec.build_modular_report(
        preset=body.preset, sections=body.sections, payload=payload,
    )
    if (body.preset or '').lower() == 'custom' and not built.get('sections'):
        raise HTTPException(400, '请至少选择一个报告章节，不能导出空报告')
    return built


@app.post('/api/reports/export')
def reports_export_v5(body: ReportExportRequest):
    fmt = _guard_report_request(body)
    try:
        payload = full_recalculate()
    except PermissionError as e:
        raise HTTPException(403, str(e))
    built = report_sec.build_modular_report(
        preset=body.preset, sections=body.sections, payload=payload,
    )
    if (body.preset or '').lower() == 'custom' and not built.get('sections'):
        raise HTTPException(400, '请至少选择一个报告章节，不能导出空报告')
    if fmt == 'csv':
        text = report_sec.build_modular_csv(payload, built['sections'])
        media = 'text/csv; charset=utf-8'
    elif fmt == 'html':
        text = report_sec.build_modular_html(payload, built['sections'], built.get('text') or '')
        media = 'text/html; charset=utf-8'
    else:
        text = built['text']
        media = 'text/plain; charset=utf-8'
    sa = (payload or {}).get('selected_assessment') or {}
    snap = (payload or {}).get('assessment_snapshot') or {}
    archive_saved = True
    archive_warning = ''
    try:
        store.archive_exported_report({
            'snapshot_id': snap.get('snapshot_id'),
            'case_id': (payload or {}).get('case', {}).get('case_id') if isinstance((payload or {}).get('case'), dict) else None,
            'case_name': (payload or {}).get('case', {}).get('name') if isinstance((payload or {}).get('case'), dict) else None,
            'format': fmt,
            'sections': built.get('sections'),
            'text': text,
            'saved_kwh': sa.get('saved_kwh'),
            'device_name': sa.get('device_name'),
            'measure_id': sa.get('measure_id'),
            'exported_at': snap.get('calculated_at') or payload.get('calculated_at'),
        })
    except Exception as exc:
        archive_saved = False
        archive_warning = f'历史归档未保存：{exc}'
    resp = PlainTextResponse(text, media_type=media)
    if not archive_saved:
        # HTTP 头只用 ASCII，避免中文头触发 Starlette UnicodeEncodeError 导致整份导出 500
        resp.headers['X-Archive-Saved'] = 'false'
        resp.headers['X-Archive-Error-Code'] = 'write_failed'
    else:
        resp.headers['X-Archive-Saved'] = 'true'
    return resp


@app.get('/api/reports/archives')
def reports_archives():
    return {'reports': store.list_exported_reports()}


@app.get('/api/reports/archives/{export_id}')
def reports_archive_one(export_id: str):
    rec = store.get_exported_report(export_id)
    if rec is None:
        raise HTTPException(404, '未找到该历史报告快照')
    return rec


@app.post('/api/reset-demo')
def reset_demo():
    """仅重置当前会话到公开示范，不影响其他访客。"""
    store.reset_session_demo()
    return {
        'ok': True,
        'results': full_recalculate(),
        'session_id': store.current_session_id(),
        'project_token': None,
    }


@app.post('/api/reports/export-full')
def reports_export_full_legacy():
    try:
        return PlainTextResponse(build_report_text(), media_type='text/plain; charset=utf-8')
    except PermissionError as e:
        raise HTTPException(403, str(e))


def _sanitize_errors(obj):
    """确保错误详情可 JSON 序列化（剔除 NaN/Inf）。"""
    if isinstance(obj, float):
        if not math.isfinite(obj):
            return str(obj)
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_errors(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_errors(v) for v in obj]
    return obj


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError):
    errs = []
    for e in exc.errors():
        loc = '.'.join(str(x) for x in e.get('loc', []) if x != 'body')
        msg = e.get('msg', 'invalid')
        errs.append(f'{loc}: {msg}' if loc else msg)
    return JSONResponse(
        status_code=400,
        content=_sanitize_errors({'detail': {'errors': errs or ['invalid_input'], 'message': 'invalid_input'}}),
    )


@app.exception_handler(ValidationError)
async def pydantic_validation_handler(request: Request, exc: ValidationError):
    return JSONResponse(
        status_code=400,
        content=_sanitize_errors({'detail': {'errors': [str(exc)], 'message': 'invalid_input'}}),
    )


_DIST = Path(__file__).resolve().parents[2] / 'frontend' / 'dist'
if _DIST.is_dir():
    app.mount('/', StaticFiles(directory=str(_DIST), html=True), name='ui')
