"""V8 设备物理/效率模型（可核对样例）。

method_code:
- pump_hydraulic
- fan_pressure_flow
- motor_efficiency
- compressor_power_drop (实测功率差，非专业气路模型)
- transformer_losses
"""
from __future__ import annotations

import math
from typing import Any


MAX_HOURS_NON_LEAP = 8760
MAX_HOURS_LEAP = 8784
G_STANDARD = 9.81  # 地面矿山场景只读重力加速度，不开放任意业务字段
RANGE_MSG = '参数超出合理范围，请检查单位'

# 工程业务上限（有限浮点 1e308 必须在运算前拦截）
FIELD_LIMITS = {
    'q_m3_h': (0.0, 1_000_000.0),
    'head_m': (0.0, 5_000.0),
    'rho_kg_m3': (1e-6, 20_000.0),
    'q_m3_s': (0.0, 100_000.0),
    'delta_p_pa': (0.0, 10_000_000.0),
    'safety_min_q_m3_s': (0.0, 100_000.0),
    'p_shaft_kw': (0.0, 100_000.0),
    'p_before_kw': (0.0, 100_000.0),
    'p_after_kw': (0.0, 100_000.0),
    'p0_before_kw': (0.0, 10_000.0),
    'p0_after_kw': (0.0, 10_000.0),
    'pk_before_kw': (0.0, 10_000.0),
    'pk_after_kw': (0.0, 10_000.0),
    'hours': (0.0, float(MAX_HOURS_LEAP)),
}

PROFESSIONAL_METHOD_CODES = (
    'pump_hydraulic',
    'fan_pressure_flow',
    'motor_efficiency',
    'compressor_power_drop',
    'transformer_losses',
)


def _invalid(method: str, msg: str, **extra: Any) -> dict[str, Any]:
    out = {
        'status': 'invalid_input',
        'method_code': method,
        'saved_kwh': None,
        'co2_reduction_t': None,
        'error': msg,
        'model_label': None,
    }
    out.update(extra)
    return out


def _ok(method: str, label: str, saved: float, *, formula: str, inputs: dict, assumptions: list[str] | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    assumptions = list(assumptions or [])
    increased = saved < 0
    if increased:
        assumptions.append('改造后用电增加，结果为增耗情景，不作节能技改优选')
    out = {
        'status': 'ok',
        'method_code': method,
        'model_label': label,
        'saved_kwh': round(saved, 2),
        'formula': formula,
        'inputs': inputs,
        'assumptions': assumptions,
        'technology_type': method,
        'calculation_method': method,
        'energy_direction': 'increase' if increased else 'save',
        'display_energy_label': '预计增加用电' if increased else '预计节电',
        'display_carbon_label': '预计增加相关排放' if increased else '预计减排',
        'increased_kwh': round(abs(saved), 2) if increased else None,
        'recommendable': (not increased) and math.isfinite(saved),
    }
    if extra:
        out.update(extra)
    return out


def _parse_finite(v: Any, name: str, *, allow_none: bool = False) -> float | None:
    if v is None:
        if allow_none:
            return None
        raise ValueError(f'{name} 不能为空')
    if isinstance(v, bool):
        raise ValueError(f'{name} 必须是数值')
    if isinstance(v, str):
        s = v.strip()
        if s.lower() in ('nan', '+nan', '-nan', 'infinity', '+infinity', '-infinity', 'inf', '+inf', '-inf'):
            raise ValueError(f'{name} 不能为 NaN/Infinity')
        try:
            f = float(s)
        except ValueError as e:
            raise ValueError(f'{name} 无法解析为数值') from e
    else:
        try:
            f = float(v)
        except (TypeError, ValueError) as e:
            raise ValueError(f'{name} 无法解析为数值') from e
    if not math.isfinite(f):
        raise ValueError(f'{name} 不能为 NaN/Infinity')
    return f


def _check_range(name: str, value: float) -> str | None:
    lim = FIELD_LIMITS.get(name)
    if lim is None:
        return None
    lo, hi = lim
    if value < lo or value > hi:
        return f'{name} {RANGE_MSG}'
    return None


def _finite_outputs(*vals: float) -> bool:
    return all(math.isfinite(v) for v in vals)


def _check_hours(hrs: float, *, year: int | None = None) -> str | None:
    """返回错误信息；None 表示通过。单年度节电情景：最多 8760/8784。"""
    if hrs < 0:
        return 'hours 不得为负'
    cap = MAX_HOURS_LEAP if year and (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else MAX_HOURS_NON_LEAP
    if hrs > cap:
        return f'年有效小时不可超过 {cap}（单年度节电情景）'
    return None


def pump_hydraulic(
    *,
    q_m3_h: float,
    head_m: float,
    rho_kg_m3: float = 1000.0,
    eta_pump_before: float,
    eta_pump_after: float,
    eta_motor: float = 0.90,
    eta_drive: float = 1.0,
    hours: float,
    year: int | None = None,
) -> dict[str, Any]:
    """P_elec = ρ g Q H / (3_600_000 ηp ηm ηd)；Q 单位 m³/h。重力用内置常量。"""
    method = 'pump_hydraulic'
    try:
        specs = [
            ('q_m3_h', q_m3_h), ('head_m', head_m), ('rho_kg_m3', rho_kg_m3),
            ('eta_pump_before', eta_pump_before), ('eta_pump_after', eta_pump_after),
            ('eta_motor', eta_motor), ('eta_drive', eta_drive), ('hours', hours),
        ]
        parsed = {n: _parse_finite(v, n) for n, v in specs}
    except ValueError as e:
        return _invalid(method, str(e))
    q, h, rho = parsed['q_m3_h'], parsed['head_m'], parsed['rho_kg_m3']
    ep0, ep1 = parsed['eta_pump_before'], parsed['eta_pump_after']
    em, ed, hrs = parsed['eta_motor'], parsed['eta_drive'], parsed['hours']
    if rho <= 0:
        return _invalid(method, f'rho_kg_m3 {RANGE_MSG}（介质密度必须大于 0）')
    for n, v in (('q_m3_h', q), ('head_m', h), ('rho_kg_m3', rho), ('hours', hrs)):
        if v < 0:
            return _invalid(method, f'{n} 不得为负')
        rerr = _check_range(n, v)
        if rerr:
            return _invalid(method, rerr)
    herr = _check_hours(hrs, year=year)
    if herr:
        return _invalid(method, herr)
    for n, e in (('eta_pump_before', ep0), ('eta_pump_after', ep1), ('eta_motor', em), ('eta_drive', ed)):
        if not (0 < e <= 1):
            return _invalid(method, f'{n} 须在 (0,1]，当前={e}')
    if q == 0 or h == 0:
        return _ok(
            method, '水泵水力模型', 0.0,
            formula='Q 或 H 为 0 → 功率为 0，节电为 0',
            inputs={'q_m3_h': q, 'head_m': h, 'hours': hrs},
            assumptions=['零流量或零扬程工况'] + (['年有效小时为 0，结果为 0'] if hrs == 0 else []),
        )
    denom0 = 3_600_000.0 * ep0 * em * ed
    denom1 = 3_600_000.0 * ep1 * em * ed
    g_f = G_STANDARD
    p0 = rho * g_f * q * h / denom0
    p1 = rho * g_f * q * h / denom1
    saved = (p0 - p1) * hrs
    if not _finite_outputs(p0, p1, saved):
        return _invalid(method, RANGE_MSG)
    assumptions = []
    if hrs == 0:
        assumptions.append('年有效小时为 0，结果为 0')
    return _ok(
        method, '水泵水力模型', saved,
        formula='P=ρgQH/(3600000·ηp·ηm·ηd); E=(P0-P1)×H',
        inputs={
            'q_m3_h': q, 'head_m': h, 'rho_kg_m3': rho,
            'eta_pump_before': ep0, 'eta_pump_after': ep1,
            'eta_motor': em, 'eta_drive': ed, 'hours': hrs,
            'p_before_kw': round(p0, 6), 'p_after_kw': round(p1, 6),
        },
        assumptions=assumptions,
        extra={'before_input_power_kw': round(p0, 6), 'after_input_power_kw': round(p1, 6)},
    )


def fan_pressure_flow(
    *,
    q_m3_s: float,
    delta_p_pa: float,
    eta_fan_before: float,
    eta_fan_after: float,
    eta_motor: float = 0.90,
    eta_drive: float = 1.0,
    hours: float,
    safety_min_q_m3_s: float | None = None,
    year: int | None = None,
) -> dict[str, Any]:
    """P = Q·Δp / (1000·ηf·ηm·ηd)。"""
    method = 'fan_pressure_flow'
    try:
        q = _parse_finite(q_m3_s, 'q_m3_s')
        dp = _parse_finite(delta_p_pa, 'delta_p_pa')
        ef0 = _parse_finite(eta_fan_before, 'eta_fan_before')
        ef1 = _parse_finite(eta_fan_after, 'eta_fan_after')
        em = _parse_finite(eta_motor, 'eta_motor')
        ed = _parse_finite(eta_drive, 'eta_drive')
        hrs = _parse_finite(hours, 'hours')
    except ValueError as e:
        return _invalid(method, str(e))
    for n, v in (('q_m3_s', q), ('delta_p_pa', dp), ('hours', hrs)):
        if v < 0:
            return _invalid(method, f'{n} 非法')
        rerr = _check_range(n, v)
        if rerr:
            return _invalid(method, rerr)
    herr = _check_hours(hrs, year=year)
    if herr:
        return _invalid(method, herr)
    for n, e in (('eta_fan_before', ef0), ('eta_fan_after', ef1), ('eta_motor', em), ('eta_drive', ed)):
        if not (0 < e <= 1):
            return _invalid(method, f'{n} 须在 (0,1]')

    safety_note = None
    qmin = None
    if safety_min_q_m3_s is None:
        safety_note = '未核验安全风量下限，不作为矿井通风调节建议'
    else:
        try:
            qmin = _parse_finite(safety_min_q_m3_s, 'safety_min_q_m3_s')
        except ValueError as e:
            return _invalid(method, f'安全风量下限非法: {e}', safety_conflict=True)
        if qmin < 0:
            return _invalid(method, '安全风量下限不得为负', safety_conflict=True)
        if qmin == 0:
            # 0 不能当作“已核验的安全下限”，只作探索性试算
            safety_note = '安全最低风量填 0 不能视为已核验，仅可查看探索性节电潜力，暂不提供实施推荐'
        elif q < qmin:
            return {
                'status': 'invalid_input',
                'method_code': method,
                'saved_kwh': None,
                'co2_reduction_t': None,
                'error': f'风量 {q} m³/s 低于安全下限 {qmin} m³/s，禁止推荐该节能方案',
                'safety_conflict': True,
                'model_label': '风机风量—风压—效率模型',
            }

    p0 = q * dp / (1000.0 * ef0 * em * ed)
    p1 = q * dp / (1000.0 * ef1 * em * ed)
    saved = (p0 - p1) * hrs
    if not _finite_outputs(p0, p1, saved):
        return _invalid(method, RANGE_MSG)
    assumptions = []
    if safety_note:
        assumptions.append(safety_note)
    if hrs == 0:
        assumptions.append('年有效小时为 0，结果为 0')
    return _ok(
        method, '风机风量—风压—效率模型', saved,
        formula='P=Q·Δp/(1000·ηf·ηm·ηd); E=(P0-P1)×H',
        inputs={
            'q_m3_s': q, 'delta_p_pa': dp,
            'eta_fan_before': ef0, 'eta_fan_after': ef1,
            'eta_motor': em, 'eta_drive': ed, 'hours': hrs,
            'safety_min_q_m3_s': qmin,
            'p_before_kw': round(p0, 6), 'p_after_kw': round(p1, 6),
        },
        assumptions=assumptions,
        extra={'before_input_power_kw': round(p0, 6), 'after_input_power_kw': round(p1, 6)},
    )


def motor_efficiency(
    *,
    p_shaft_kw: float,
    eta_old: float,
    eta_new: float,
    hours: float,
    year: int | None = None,
) -> dict[str, Any]:
    """E = P_shaft × H × (1/η_old − 1/η_new)。轴功率，不是电机输入功率。"""
    method = 'motor_efficiency'
    try:
        ps = _parse_finite(p_shaft_kw, 'p_shaft_kw')
        e0 = _parse_finite(eta_old, 'eta_old')
        e1 = _parse_finite(eta_new, 'eta_new')
        hrs = _parse_finite(hours, 'hours')
    except ValueError as e:
        return _invalid(method, str(e))
    for n, v in (('p_shaft_kw', ps), ('hours', hrs)):
        if v < 0:
            return _invalid(method, f'{n} 非法')
        rerr = _check_range(n, v)
        if rerr:
            return _invalid(method, rerr)
    herr = _check_hours(hrs, year=year)
    if herr:
        return _invalid(method, herr)
    for n, e in (('eta_old', e0), ('eta_new', e1)):
        if not (0 < e <= 1):
            return _invalid(method, f'{n} 须在 (0,1]')
    saved = ps * hrs * (1.0 / e0 - 1.0 / e1)
    if not _finite_outputs(saved):
        return _invalid(method, RANGE_MSG)
    assumptions = ['相同机械轴输出功率']
    if hrs == 0:
        assumptions.append('年有效小时为 0，结果为 0')
    p_in0 = ps / e0
    p_in1 = ps / e1
    return _ok(
        method, '电机效率升级模型', saved,
        formula='E=P_shaft×H×(1/η_old−1/η_new)',
        inputs={
            'p_shaft_kw': ps, 'eta_old': e0, 'eta_new': e1, 'hours': hrs,
            'before_input_power_kw': round(p_in0, 6),
            'after_input_power_kw': round(p_in1, 6),
        },
        assumptions=assumptions + ['轴功率不是电机输入功率；输入功率 = 轴功率 / 效率'],
        extra={
            'before_input_power_kw': round(p_in0, 6),
            'after_input_power_kw': round(p_in1, 6),
            'shaft_power_kw': round(ps, 6),
        },
    )


def compressor_power_drop(
    *,
    p_before_kw: float,
    p_after_kw: float,
    hours: float,
    same_duty: bool = True,
    year: int | None = None,
) -> dict[str, Any]:
    """空压机：同供气任务下实测功率差×小时（低数据路径，非专业工况模型）。"""
    method = 'compressor_power_drop'
    try:
        pb = _parse_finite(p_before_kw, 'p_before_kw')
        pa = _parse_finite(p_after_kw, 'p_after_kw')
        hrs = _parse_finite(hours, 'hours')
    except ValueError as e:
        return _invalid(method, str(e))
    for n, v in (('p_before_kw', pb), ('p_after_kw', pa), ('hours', hrs)):
        if v < 0:
            return _invalid(method, f'{n} 非法')
        rerr = _check_range(n, v)
        if rerr:
            return _invalid(method, rerr)
    herr = _check_hours(hrs, year=year)
    if herr:
        return _invalid(method, herr)
    if not same_duty:
        return _invalid(method, '供气任务不可比，不能直接取功率差')
    saved = (pb - pa) * hrs
    if not _finite_outputs(saved):
        return _invalid(method, RANGE_MSG)
    assumptions = ['同供气量、同压力任务可比', '参考情景·非专业气路模型']
    if hrs == 0:
        assumptions.append('年有效小时为 0，结果为 0')
    return _ok(
        method, '空压机实测功率差情景', saved,
        formula='E=(P_before−P_after)×H（同供气量）',
        inputs={'p_before_kw': pb, 'p_after_kw': pa, 'hours': hrs},
        assumptions=assumptions,
        extra={'before_input_power_kw': round(pb, 6), 'after_input_power_kw': round(pa, 6)},
    )


def transformer_losses(
    *,
    p0_before_kw: float,
    p0_after_kw: float,
    pk_before_kw: float,
    pk_after_kw: float,
    beta: float,
    hours: float,
    year: int | None = None,
) -> dict[str, Any]:
    """P2 简化：E = (P0b−P0a)×T + (Pkb−Pka)×β²×T。"""
    method = 'transformer_losses'
    try:
        p0b = _parse_finite(p0_before_kw, 'p0_before_kw')
        p0a = _parse_finite(p0_after_kw, 'p0_after_kw')
        pkb = _parse_finite(pk_before_kw, 'pk_before_kw')
        pka = _parse_finite(pk_after_kw, 'pk_after_kw')
        b = _parse_finite(beta, 'beta')
        hrs = _parse_finite(hours, 'hours')
    except ValueError as e:
        return _invalid(method, str(e))
    for n, v in (
        ('p0_before_kw', p0b), ('p0_after_kw', p0a),
        ('pk_before_kw', pkb), ('pk_after_kw', pka), ('hours', hrs),
    ):
        if v < 0:
            return _invalid(method, f'{n} 不得为负')
        rerr = _check_range(n, v)
        if rerr:
            return _invalid(method, rerr)
    if b < 0 or b > 1:
        return _invalid(method, '负载系数须在 0—1')
    herr = _check_hours(hrs, year=year)
    if herr:
        return _invalid(method, herr)
    saved = (p0b - p0a) * hrs + (pkb - pka) * (b ** 2) * hrs
    if not _finite_outputs(saved):
        return _invalid(method, RANGE_MSG)
    assumptions = []
    if hrs == 0:
        assumptions.append('年有效小时为 0，结果为 0')
    return _ok(
        method, '变压器损耗简化模型', saved,
        formula='E=(P0b−P0a)×T+(Pkb−Pka)×β²×T',
        inputs={
            'p0_before_kw': p0b, 'p0_after_kw': p0a,
            'pk_before_kw': pkb, 'pk_after_kw': pka,
            'beta': b, 'hours': hrs,
        },
        assumptions=assumptions,
    )


def run_method(method_code: str, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    """统一入口：按 method_code 调用对应模型。未知方法返回 invalid。"""
    if method_code is not None and not isinstance(method_code, str):
        return _invalid('unknown', 'method_code 必须是字符串')
    body = dict(inputs or {})
    body.pop('method_code', None)
    body.pop('g', None)  # 重力只读常量，忽略用户传入的任意 g
    year = body.get('year') or body.get('accounting_year')
    if year is not None and 'year' not in body:
        body['year'] = year
    dispatch = {
        'pump_hydraulic': pump_hydraulic,
        'fan_pressure_flow': fan_pressure_flow,
        'motor_efficiency': motor_efficiency,
        'compressor_power_drop': compressor_power_drop,
        'transformer_losses': transformer_losses,
    }
    fn = dispatch.get(method_code)
    if fn is None:
        return _invalid(method_code or 'unknown', f'暂不支持或未知 method_code: {method_code}')
    # 过滤函数未接受的多余键，避免 TypeError→500
    import inspect
    sig = inspect.signature(fn)
    allowed = set(sig.parameters.keys())
    kwargs = {k: v for k, v in body.items() if k in allowed}
    try:
        return fn(**kwargs)
    except TypeError as e:
        return _invalid(method_code, f'参数不匹配: {e}')
