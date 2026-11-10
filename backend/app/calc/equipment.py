"""球磨机 / 水泵节能情景核算（统一权威函数）。

状态约定：
- 0 = 已知真实零值
- None/null = 未提供，不能计算绝对前后值
- status: ok | insufficient_data | invalid_input
"""
from __future__ import annotations
import math

JH_2025_COST_10K_CNY = 6191.20
JH_2025_ELEC_10K_KWH = 13612.92
COST_INTENSITY_CNY_PER_KWH = JH_2025_COST_10K_CNY / JH_2025_ELEC_10K_KWH
GAN_SU_GRID_FACTOR_2023_KGCO2_PER_KWH = 0.4471
PUBLISHED_POWER_DROP_KW = 200.0
PUBLISHED_PUMP_RATE = 0.1293
MAX_ANNUAL_HOURS = 8760  # 非闰年默认上限；闰年用 max_hours_for_year
MAX_ANNUAL_HOURS_LEAP = 8784


def max_hours_for_year(year: int | None = None) -> int:
    """非闰年 8760，闰年 8784。"""
    if year is None:
        return MAX_ANNUAL_HOURS
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    return MAX_ANNUAL_HOURS_LEAP if leap else MAX_ANNUAL_HOURS



def mill_scenario(
    *,
    delta_power_kw: float | None,
    hours: float | None,
    baseline_power_kw: float | None = None,
    tariff_cny_per_kwh: float | None = COST_INTENSITY_CNY_PER_KWH,
    ef_kgco2_per_kwh: float = GAN_SU_GRID_FACTOR_2023_KGCO2_PER_KWH,
    investment_cny: float | None = None,
    annual_extra_om_cny: float | None = 0,
    source_type: str = 'scenario',
    tariff_is_reference: bool = False,
    calendar_year: int | None = None,
    max_hours: int | None = None,
) -> dict:
    """球磨机情景。缺少基准功率时 before/after 为 null，saved 仍可算。
    无电价时节电/减排可算，节费/净收益/回收期为 null（禁止静默套金徽电价）。
    """
    missing_required: list[str] = []
    if delta_power_kw is None:
        missing_required.append('delta_power_kw')
    if hours is None:
        missing_required.append('hours')
    if missing_required:
        return {
            'status': 'insufficient_data',
            'source_type': source_type,
            'saved_kwh': None,
            'before_kwh': None,
            'after_kwh': None,
            'co2_reduction_t': None,
            'annual_cost_saving_cny': None,
            'annual_net_saving_cny': None,
            'simple_payback_years': None,
            'payback_message': None,
            'missing_for_absolute_comparison': ['baseline_power_kw'] if baseline_power_kw is None else [],
            'missing_required': missing_required,
            'assumptions': [],
            'factor_method': 'Gansu grid average factor, 2023',
            'error': f'缺少必需字段: {", ".join(missing_required)}',
            'annual_electricity_saved_kwh': None,
            'scenario_grid_co2_saved_t': None,
            'scenario_electricity_cost_saved_cny': None,
            'is_actual_annual_savings': False,
            'tariff_missing': True,
            'cost_note': '补充电价后可估算节省费用',
        }

    try:
        delta = float(delta_power_kw)
        hrs = float(hours)
        ef = float(ef_kgco2_per_kwh)
        om = 0.0 if annual_extra_om_cny is None else float(annual_extra_om_cny)
    except (TypeError, ValueError) as e:
        return _invalid(f'数值无法解析: {e}', source_type)

    tariff: float | None
    if tariff_cny_per_kwh is None:
        tariff = None
    else:
        try:
            tariff = float(tariff_cny_per_kwh)
        except (TypeError, ValueError) as e:
            return _invalid(f'电价无法解析: {e}', source_type)

    for name, val in (
        ('delta_power_kw', delta), ('hours', hrs), ('ef_kgco2_per_kwh', ef),
        ('annual_extra_om_cny', om),
    ):
        if not math.isfinite(val):
            return _invalid(f'{name} 不能为 NaN/Infinity', source_type)
    if tariff is not None and not math.isfinite(tariff):
        return _invalid('电价不能为 NaN/Infinity', source_type)

    if delta < 0 or hrs < 0 or ef < 0 or om < 0:
        return _invalid('输入不能为负（小时、功率降幅、因子、运维费）', source_type)
    if tariff is not None and tariff < 0:
        return _invalid('输入不能为负（电价）', source_type)
    if max_hours is not None:
        hours_cap = max_hours
    elif calendar_year is not None:
        hours_cap = max_hours_for_year(calendar_year)
    else:
        # 未指定年份时允许闰年绝对上限，避免误杀 8784
        hours_cap = MAX_ANNUAL_HOURS_LEAP
    if hrs > hours_cap:
        return _invalid(f'年有效小时不可超过 {hours_cap}', source_type)

    inv: float | None
    if investment_cny is None:
        inv = None
    else:
        try:
            inv = float(investment_cny)
        except (TypeError, ValueError) as e:
            return _invalid(f'投资无法解析: {e}', source_type)
        if not math.isfinite(inv):
            return _invalid('投资不能为 NaN/Infinity', source_type)
        if inv < 0:
            return _invalid('投资不得为负', source_type)

    baseline: float | None
    if baseline_power_kw is None:
        baseline = None
    else:
        try:
            baseline = float(baseline_power_kw)
        except (TypeError, ValueError) as e:
            return _invalid(f'基准功率无法解析: {e}', source_type)
        if not math.isfinite(baseline):
            return _invalid('基准功率不能为 NaN/Infinity', source_type)
        if baseline < 0:
            return _invalid('基准功率不得为负', source_type)

    saved_kwh = round(delta * hrs, 2)
    co2 = round(saved_kwh * ef / 1000, 3)

    cost = None
    net = None
    cost_note = None
    if tariff is None:
        cost_note = '补充电价后可估算节省费用'
    else:
        cost = round(saved_kwh * tariff, 2)
        net = round(cost - om, 2)
        if tariff_is_reference:
            cost_note = '参考价格、并非本企业实际电价'

    before_kwh = None
    after_kwh = None
    missing_abs: list[str] = []
    if baseline is None:
        missing_abs.append('baseline_power_kw')
    elif baseline >= delta:
        before_kwh = round(baseline * hrs, 2)
        after_kwh = round((baseline - delta) * hrs, 2)
    else:
        return _invalid('基准功率须 ≥ 功率降幅，才能计算改造前后绝对能耗', source_type)

    payback = None
    payback_message = None
    if tariff is None:
        payback_message = '补充电价后可估算节省费用'
    elif inv is None:
        payback_message = '待输入投资'
    elif net is None or net <= 0:
        payback = None
        payback_message = '当前假设下无法依靠节能回本'
    else:
        payback = round(inv / net, 2)

    assumptions: list[str] = []
    if hrs == 4000:
        assumptions.append('hours=4000')
    if inv is not None and inv == 1_000_000:
        assumptions.append('investment=1000000')
    if om == 0:
        assumptions.append('annual_extra_om_cny=0')
    if baseline is not None:
        assumptions.append(f'baseline_power_kw={baseline}')
    if tariff_is_reference and tariff is not None:
        assumptions.append('tariff=jinhui_reference')

    return {
        'status': 'ok',
        'source_type': source_type,
        'saved_kwh': saved_kwh,
        'before_kwh': before_kwh,
        'after_kwh': after_kwh,
        'co2_reduction_t': co2,
        'annual_cost_saving_cny': cost,
        'annual_net_saving_cny': net,
        'simple_payback_years': payback,
        'payback_message': payback_message,
        'missing_for_absolute_comparison': missing_abs,
        'missing_required': [],
        'assumptions': assumptions,
        'factor_method': 'Gansu grid average factor, 2023',
        'inputs': {
            'delta_power_kw': delta,
            'hours': hrs,
            'baseline_power_kw': baseline,
            'tariff_cny_per_kwh': tariff,
            'ef_kgco2_per_kwh': ef,
            'investment_cny': inv,
            'annual_extra_om_cny': om,
        },
        'formula': 'saved_kwh = delta_power_kw × hours; co2_t = saved_kwh × ef / 1000',
        'data_type': '模型估算',
        'annual_electricity_saved_kwh': saved_kwh,
        'scenario_grid_co2_saved_t': co2,
        'scenario_electricity_cost_saved_cny': cost,
        'is_actual_annual_savings': False,
        'mode': 'mill',
        'tariff_missing': tariff is None,
        'tariff_is_reference': bool(tariff_is_reference and tariff is not None),
        'cost_note': cost_note,
    }


def _invalid(msg: str, source_type: str) -> dict:
    return {
        'status': 'invalid_input',
        'source_type': source_type,
        'saved_kwh': None,
        'before_kwh': None,
        'after_kwh': None,
        'co2_reduction_t': None,
        'annual_cost_saving_cny': None,
        'annual_net_saving_cny': None,
        'simple_payback_years': None,
        'payback_message': None,
        'missing_for_absolute_comparison': [],
        'missing_required': [],
        'assumptions': [],
        'factor_method': 'Gansu grid average factor, 2023',
        'error': msg,
        'annual_electricity_saved_kwh': None,
        'scenario_grid_co2_saved_t': None,
        'scenario_electricity_cost_saved_cny': None,
        'is_actual_annual_savings': False,
    }


def mill_saving(
    hours,
    power_drop_kw=PUBLISHED_POWER_DROP_KW,
    cost_intensity=COST_INTENSITY_CNY_PER_KWH,
    emission_factor=GAN_SU_GRID_FACTOR_2023_KGCO2_PER_KWH,
    investment_cny=None,
    annual_maintenance_cny=0,
    baseline_power_kw=None,
):
    """兼容旧调用签名；内部走 mill_scenario。非法输入仍抛 ValueError 以兼容旧测试。"""
    r = mill_scenario(
        delta_power_kw=power_drop_kw,
        hours=hours,
        baseline_power_kw=baseline_power_kw,
        tariff_cny_per_kwh=cost_intensity,
        ef_kgco2_per_kwh=emission_factor,
        investment_cny=investment_cny,
        annual_extra_om_cny=annual_maintenance_cny,
    )
    if r['status'] == 'invalid_input':
        raise ValueError(r.get('error') or 'Invalid input')
    if r['status'] == 'insufficient_data':
        raise ValueError(r.get('error') or 'Insufficient data')
    # 旧返回形状
    out = {
        'annual_electricity_saved_kwh': r['saved_kwh'],
        'scenario_electricity_cost_saved_cny': r['annual_cost_saving_cny'],
        'scenario_grid_co2_saved_t': r['co2_reduction_t'],
        'is_actual_annual_savings': False,
        'before_kwh': r['before_kwh'],
        'after_kwh': r['after_kwh'],
        'status': r['status'],
        'simple_payback_years': r['simple_payback_years'],
        'payback_message': r['payback_message'],
    }
    return out


def pump_scenario(
    *,
    pump_baseline_kwh: float | None,
    saving_fraction: float = PUBLISHED_PUMP_RATE,
    ef_kgco2_per_kwh: float = GAN_SU_GRID_FACTOR_2023_KGCO2_PER_KWH,
    tariff_cny_per_kwh: float | None = COST_INTENSITY_CNY_PER_KWH,
    tariff_is_reference: bool = False,
) -> dict:
    """水泵独立模块。无基准时仅提示，不算。无电价时节费为 null。"""
    if pump_baseline_kwh is None:
        return {
            'status': 'insufficient_data',
            'saved_kwh': None,
            'co2_reduction_t': None,
            'annual_cost_saving_cny': None,
            'message': '待输入设备基准年电耗',
            'error': '待输入设备基准年电耗',
            'annual_electricity_saved_kwh': None,
            'is_actual_annual_savings': False,
            'mode': 'pump',
            'tariff_missing': True,
            'cost_note': '补充电价后可估算节省费用',
        }
    try:
        base = float(pump_baseline_kwh)
        frac = float(saving_fraction)
        ef = float(ef_kgco2_per_kwh)
    except (TypeError, ValueError) as e:
        return {
            'status': 'invalid_input',
            'saved_kwh': None,
            'co2_reduction_t': None,
            'annual_cost_saving_cny': None,
            'message': str(e),
            'error': f'水泵输入无效: {e}',
            'annual_electricity_saved_kwh': None,
            'is_actual_annual_savings': False,
            'mode': 'pump',
        }
    tariff: float | None
    if tariff_cny_per_kwh is None:
        tariff = None
    else:
        try:
            tariff = float(tariff_cny_per_kwh)
        except (TypeError, ValueError) as e:
            return {
                'status': 'invalid_input',
                'saved_kwh': None,
                'co2_reduction_t': None,
                'annual_cost_saving_cny': None,
                'message': str(e),
                'error': f'水泵电价无效: {e}',
                'annual_electricity_saved_kwh': None,
                'is_actual_annual_savings': False,
                'mode': 'pump',
            }
    for name, val in (('pump_baseline_kwh', base), ('saving_fraction', frac), ('ef_kgco2_per_kwh', ef)):
        if not math.isfinite(val):
            return {
                'status': 'invalid_input',
                'saved_kwh': None,
                'co2_reduction_t': None,
                'annual_cost_saving_cny': None,
                'message': f'{name} 不能为 NaN/Infinity',
                'error': f'{name} 不能为 NaN/Infinity',
                'annual_electricity_saved_kwh': None,
                'is_actual_annual_savings': False,
                'mode': 'pump',
            }
    if tariff is not None and not math.isfinite(tariff):
        return {
            'status': 'invalid_input',
            'saved_kwh': None,
            'co2_reduction_t': None,
            'annual_cost_saving_cny': None,
            'message': '电价不能为 NaN/Infinity',
            'error': '电价不能为 NaN/Infinity',
            'annual_electricity_saved_kwh': None,
            'is_actual_annual_savings': False,
            'mode': 'pump',
        }
    if base < 0 or not (0 <= frac <= 1) or ef < 0 or (tariff is not None and tariff < 0):
        return {
            'status': 'invalid_input',
            'saved_kwh': None,
            'co2_reduction_t': None,
            'annual_cost_saving_cny': None,
            'message': 'Invalid pump input',
            'error': 'Invalid pump input',
            'annual_electricity_saved_kwh': None,
            'is_actual_annual_savings': False,
            'mode': 'pump',
        }
    saved = round(base * frac, 2)
    co2 = round(saved * ef / 1000, 3)
    cost = None if tariff is None else round(saved * tariff, 2)
    cost_note = None
    if tariff is None:
        cost_note = '补充电价后可估算节省费用'
    elif tariff_is_reference:
        cost_note = '参考价格、并非本企业实际电价'
    frac_pct = f'{frac * 100:.4g}%'
    return {
        'status': 'ok',
        'saved_kwh': saved,
        'co2_reduction_t': co2,
        'annual_cost_saving_cny': cost,
        'message': None,
        'saving_fraction': frac,
        'pump_baseline_kwh': base,
        'formula': f'pump_saved_kwh = pump_baseline_kwh × {frac}（当前节电率 {frac_pct}）',
        'data_type': '模型估算',
        'annual_electricity_saved_kwh': saved,
        'scenario_grid_co2_saved_t': co2,
        'scenario_electricity_cost_saved_cny': cost,
        'is_actual_annual_savings': False,
        'mode': 'pump',
        'inputs': {
            'baseline_device_kwh': base,
            'saving_fraction': frac,
            'tariff_cny_per_kwh': tariff,
        },
        'tariff_missing': tariff is None,
        'tariff_is_reference': bool(tariff_is_reference and tariff is not None),
        'cost_note': cost_note,
    }


def pump_saving(baseline_device_kwh, saving_fraction=PUBLISHED_PUMP_RATE):
    """兼容旧签名；非法抛 ValueError。"""
    if baseline_device_kwh is None:
        raise ValueError('Invalid pump input')
    if baseline_device_kwh < 0 or not (0 <= saving_fraction <= 1):
        raise ValueError('Invalid pump input')
    return baseline_device_kwh * saving_fraction
