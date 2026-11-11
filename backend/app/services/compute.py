"""统一核算编排：上传或改参后重算全部依赖指标。"""
from __future__ import annotations
from datetime import datetime
from typing import Any

from .store import get_case, infer_data_grade, build_data_prep, case_public_dict
from ..models.schema import CaseState
from ..calc.equipment import (
    mill_scenario, pump_scenario,
    COST_INTENSITY_CNY_PER_KWH, PUBLISHED_PUMP_RATE,
)
from ..calc.carbon import estimate_grid_co2_t, factor_meta_for, DEFAULT_FACTOR
from ..calc.retrofit_sim import aggregate_enterprise_energy
from ..calc.mill_mars import mill_power_kw, EXAMPLE
from ..calc import physics_models as phys
from ..calc.physics_models import PROFESSIONAL_METHOD_CODES
from . import equipment_catalog as equip_cat


DEMO_BASELINE_KW = 2400.0


def _effective_baseline(sc) -> float | None:
    if sc.use_demo_baseline and sc.baseline_power_kw is None:
        return DEMO_BASELINE_KW
    return sc.baseline_power_kw


def _device_by_id(case: CaseState, device_id: str | None):
    if not device_id:
        return None
    return next((d for d in case.devices if d.id == device_id), None)


def _devices_of_type(case: CaseState, *keywords: str):
    out = []
    for d in case.devices:
        blob = f"{d.device_type or ''}{d.name or ''}"
        if any(k in blob for k in keywords):
            out.append(d)
    return out


def _resolve_selected_measure(case: CaseState):
    """返回当前有效措施。selected_measure_id 指向已删除措施时视为失效（不回落到旧功率）。"""
    sc = case.scenario
    mid = sc.selected_measure_id
    if mid:
        m = next((r for r in case.retrofits if r.id == mid), None)
        if m:
            return m
        # P0-01：孤儿选中 — 不假装还有措施，禁止沿用情景里残留的 200kW
        return None
    # 示范默认球磨措施（仅未显式选中时）
    if case.is_demo or sc.use_jinhui_reference:
        m = next((r for r in case.retrofits if r.power_drop_kw is not None), None)
        return m
    return None


def _orphan_selected_measure(case: CaseState) -> bool:
    mid = case.scenario.selected_measure_id
    if not mid:
        return False
    return not any(r.id == mid for r in case.retrofits)



def _device_category(device) -> str:
    """设备类型（与计算方法分离）。V8：扩展矿山设备族识别。"""
    if device is None:
        return 'unknown'
    blob = f"{device.device_type or ''}{device.name or ''}"
    if any(k in blob for k in ('半自磨', 'SAG', 'sag')):
        return 'sag'
    if any(k in blob for k in ('球磨', '磨机')):
        return 'mill'
    if any(k in blob for k in ('空压', '压缩')):
        return 'compressor'
    if any(k in blob for k in ('破碎',)):
        return 'crusher'
    # 泵优先于「输送」：名称如「二级输送泵」仍是泵
    if '泵' in blob:
        return 'pump'
    if any(k in blob for k in ('输送机', '皮带机', '带式输送')):
        return 'conveyor'
    if any(k in blob for k in ('电机', '电动机')) and '风机' not in blob:
        return 'motor'
    if any(k in blob for k in ('变压器',)):
        return 'transformer'
    if any(k in blob for k in ('风机', '风扇', '通风')):
        return 'fan'
    return 'generic'


def _calculation_method(measure, case: CaseState | None = None) -> str:
    """计算方法白名单键：通用 + 专业 method_code | unsupported。"""
    if measure is None:
        return 'unsupported'
    explicit = getattr(measure, 'method_code', None) or getattr(measure, 'calculation_method', None)
    if explicit in ('power_drop_hours', 'baseline_fraction') or explicit in PROFESSIONAL_METHOD_CODES:
        return explicit
    if measure.power_drop_kw is not None and measure.saving_fraction is None:
        return 'power_drop_hours'
    if measure.saving_fraction is not None and measure.power_drop_kw is None:
        return 'baseline_fraction'
    if measure.power_drop_kw is not None and measure.saving_fraction is not None:
        # 双字段却无显式方法：上层应已拒绝；此处视为不支持，禁止瞎算
        return 'unsupported'
    return 'unsupported'


def _is_pump_measure(measure, case: CaseState | None = None) -> bool:
    """仅当目标设备确认为泵、且采用比例节电法时为 True。不得靠名称猜测把风机当成泵。"""
    if measure is None or case is None:
        return False
    if measure.saving_fraction is None:
        return False
    if not measure.device_id:
        # 无设备绑定时：仅示范泵措施名/目标含泵时兼容
        blob = f"{measure.name or ''}{measure.target_device or ''}"
        return '泵' in blob
    dev = _device_by_id(case, measure.device_id)
    return _device_category(dev) == 'pump'


def _technology_type(measure, case: CaseState | None = None) -> str:
    """技术展示类型：pump / process / vfd / generic_fraction / professional_* / unsupported / unknown。"""
    if measure is None:
        return 'unknown'
    method = _calculation_method(measure, case)
    device = _device_by_id(case, measure.device_id) if case is not None and measure.device_id else None
    cat = _device_category(device)
    name = measure.name or ''
    if method in PROFESSIONAL_METHOD_CODES:
        return f'professional_{method}'
    if method == 'power_drop_hours':
        # 水泵功率差按「通用实测功率差情景」计算，不得创建成功后算不出来
        if '变频' in name:
            return 'vfd'
        return 'process'
    if method == 'baseline_fraction':
        if cat == 'pump':
            return 'pump'
        # 通用比例情景：风机/其他；明确标注，不伪装水泵
        return 'generic_fraction'
    return 'unsupported'


def _max_hours_for_year(year: int | None = None) -> int:
    """非闰年 8760，闰年 8784。默认按非闰年。"""
    if year is None:
        return 8760
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    return 8784 if leap else 8760


def _resolve_annual_om(case: CaseState, measure) -> tuple[float | None, str]:
    """年新增运维：优先措施自身；情景值仅在显式绑定该措施时用。
    返回 (om_value_or_None, assumption_label)。
    None=未知（阻断确定回本）；0=已确认无新增或用户明确「暂按0元试算」。
    """
    sc = case.scenario
    assume0 = bool(getattr(sc, 'assume_zero_maintenance', False))
    if measure is not None:
        if measure.annual_maintenance_cny is not None:
            if float(measure.annual_maintenance_cny) == 0:
                return 0.0, ('假设无新增运维（用户确认）' if assume0 else '措施填写·无新增运维')
            return float(measure.annual_maintenance_cny), '措施填写'
        bound = getattr(sc, 'maintenance_bound_measure_id', None)
        if bound == measure.id and sc.annual_maintenance_cny is not None:
            return float(sc.annual_maintenance_cny), '情景绑定到本措施'
        # 仅对绑定/当前选中措施生效，避免示范复制带来的标记误用于新措施
        if assume0 and (
            bound == measure.id
            or (bound is None and getattr(sc, 'selected_measure_id', None) == measure.id)
        ):
            return 0.0, '假设无新增运维（用户确认暂按0元试算）'
        # 措施未填运维：未知，不静默当 0
        return None, '年新增运维未知'
    if sc.annual_maintenance_cny is not None:
        return float(sc.annual_maintenance_cny), '情景运维'
    if assume0:
        return 0.0, '假设无新增运维（用户确认暂按0元试算）'
    return None, '年新增运维未知'


SAFETY_UNVERIFIED_MSG = (
    '可以先查看节能潜力，但通风安全条件尚未确认，暂不提供实施推荐。'
)
POWER_MISMATCH_MSG = '设备规格与工况计算矛盾'
POWER_MISMATCH_USER_MSG = '设备登记功率与当前运行条件对不上，请核对功率、流量和效率后重算。'
MEASURED_POWER_DROP_NOTE = '同流量/扬程/输送任务下的通用实测功率差情景'


def _fmt_hours_zh(v: float) -> str:
    if float(v) == int(float(v)):
        return f'{int(float(v)):,}'
    return f'{float(v):,.2f}'


def _format_scheme_txt_line(s: dict) -> str:
    """小时对照行：无效情景写明不适用，禁止输出 None kWh / None t 等程序占位。"""
    reason = s.get('inapplicable_reason') or (
        s.get('error') if s.get('status') == 'invalid_input' else None
    )
    if reason or s.get('annual_electricity_saved_kwh') is None:
        note = reason or s.get('payback_message') or s.get('error') or '超过该设备可用运行小时，不适用'
        return f"- {s.get('hours')} 小时：{note}"
    cost = s.get('scenario_electricity_cost_saved_cny')
    cost_txt = '尚待补充' if cost is None else cost
    return (
        f"- {s.get('hours')} 小时：节电 {s.get('annual_electricity_saved_kwh')} kWh，"
        f"减排 {s.get('scenario_grid_co2_saved_t')} t，"
        f"节费 {cost_txt}"
    )


def _hours_conflict_message(model_hours=None, device_hours=None) -> str:
    base = '预计节能小时超过设备实际运行时间，请重新填写'
    if model_hours is not None and device_hours is not None:
        return (
            f'预计节能{_fmt_hours_zh(model_hours)}小时，但这台设备一年只运行'
            f'{_fmt_hours_zh(device_hours)}小时。请检查时间，暂不能给出可靠的减排和回本建议。'
            f'（{base}）'
        )
    return base


def _professional_hours(measure) -> float | None:
    if measure is None:
        return None
    inputs = getattr(measure, 'model_inputs', None) or {}
    if not isinstance(inputs, dict):
        return None
    raw = inputs.get('hours')
    if raw is None or raw == '':
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _usage_is_general_industrial(measure, device) -> bool:
    inputs = dict(getattr(measure, 'model_inputs', None) or {}) if measure else {}
    usage = str(inputs.get('usage_class') or inputs.get('safety_duty') or '').strip()
    if usage in ('general_industrial', '一般工业风机', 'non_safety_critical'):
        return True
    return False


def _is_mine_safety_fan(device) -> bool:
    """以设备分类判定矿井安全通风机，不被自由文本标签绕过。"""
    if device is None:
        return True
    code = equip_cat.resolve_type_code(getattr(device, 'device_type', None) or '')
    if code == 'fan_ventilation':
        return True
    blob = f"{getattr(device, 'device_type', '') or ''} {getattr(device, 'name', '') or ''}"
    return ('矿井' in blob and '通风' in blob) or ('主扇' in blob)


def _fan_safety_unverified(measure, device) -> bool:
    """矿井主扇等安全关键通风：最低安全风量必须 >0 且不高于实际风量才算已核验。"""
    if measure is None:
        return False
    method = getattr(measure, 'method_code', None) or getattr(measure, 'calculation_method', None)
    if method != 'fan_pressure_flow':
        return False
    # 一般工业风机可豁免；矿井主扇分类不得靠 usage_class / 标签绕过
    if not _is_mine_safety_fan(device) and _usage_is_general_industrial(measure, device):
        return False
    inputs = dict(getattr(measure, 'model_inputs', None) or {})
    qmin = inputs.get('safety_min_q_m3_s')
    if qmin is None or qmin == '':
        return True
    try:
        qmin_f = float(qmin)
    except (TypeError, ValueError):
        return True
    if qmin_f <= 0:
        return True
    q = inputs.get('q_m3_s')
    try:
        if q is not None and qmin_f > float(q):
            return True
    except (TypeError, ValueError):
        return True
    return False


def _extract_before_input_power(phys_out: dict | None, measure=None) -> float | None:
    """改造前电输入功率。轴功率必须先除以效率，不得直接当输入功率。"""
    src = dict(phys_out or {})
    inputs = dict(src.get('inputs') or {})
    for key in ('before_input_power_kw',):
        if src.get(key) is not None:
            try:
                return float(src[key])
            except (TypeError, ValueError):
                pass
    if inputs.get('p_before_kw') is not None:
        try:
            return float(inputs['p_before_kw'])
        except (TypeError, ValueError):
            pass
    if inputs.get('p_shaft_kw') is not None and inputs.get('eta_old'):
        try:
            eta = float(inputs['eta_old'])
            if eta > 0:
                return float(inputs['p_shaft_kw']) / eta
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    if measure is not None:
        method = getattr(measure, 'method_code', None) or getattr(measure, 'calculation_method', None)
        if method in PROFESSIONAL_METHOD_CODES:
            raw = phys.run_method(method, dict(getattr(measure, 'model_inputs', None) or {}))
            if raw is not phys_out:
                return _extract_before_input_power(raw, None)
    return None


def _calc_vs_nameplate_warning(measure, device, phys_out: dict | None = None) -> str | None:
    if measure is None or device is None or getattr(device, 'power_kw', None) is None:
        return None
    try:
        rated = float(device.power_kw)
    except (TypeError, ValueError):
        return None
    if rated <= 0:
        return None
    try:
        p_calc_f = _extract_before_input_power(phys_out, measure)
    except (TypeError, ValueError):
        return None
    if p_calc_f is None:
        return None
    if p_calc_f > rated * 1.5 + 1e-6:
        return (
            f'{POWER_MISMATCH_USER_MSG}'
            f'（{POWER_MISMATCH_MSG}：工况计算改造前电输入约 {p_calc_f:.2f} kW，'
            f'但设备档案功率为 {rated:.2f} kW。不能给出确定的经济推荐。）'
        )
    return None


def _measure_bound_investment(measure) -> float | None:
    """投资只认措施自身；不得跨措施继承情景投资。"""
    if measure is None:
        return None
    return measure.investment_cny


def _human_waiting(tech: str, *, pump_baseline_missing=False, hours_missing=False,
                   power_missing=False, tariff_missing=False, investment_missing=False,
                   factor_missing=False) -> str | None:
    if tech == 'pump' and pump_baseline_missing:
        return '请填写这台水泵改造前一年的用电量，才能算它能省多少电。'
    if tech in ('process', 'vfd', 'mill', 'other') and power_missing:
        return '还差功率降幅。填写改造后预计少用多少功率后再计算。'
    if tech in ('process', 'vfd', 'mill', 'other') and hours_missing:
        return '还差运行时间。填入预计每年能节省电的小时数后再计算。'
    if factor_missing:
        return '已算出节电量。选择排放换算依据后，可以继续估算减碳量。'
    if tariff_missing:
        return '还不知道每度电多少钱。填写企业电价，或选择参考价试算。'
    if investment_missing:
        return '补齐投资和每年的额外费用，再看大约多久回本。'
    return None



def _enrich_phys_with_carbon_cost(
    phys_out: dict,
    *,
    ef,
    tariff,
    tariff_is_reference: bool,
) -> dict:
    """把物理模型节电结果套上电力相关减排与节费（改电价只改费用）。"""
    out = dict(phys_out)
    saved = out.get('saved_kwh')
    if out.get('status') != 'ok' or saved is None:
        out.setdefault('co2_reduction_t', None)
        out.setdefault('annual_cost_saving_cny', None)
        out.setdefault('tariff_missing', tariff is None)
        out.setdefault('tariff_is_reference', False)
        return out
    try:
        co2 = round(float(saved) * float(ef) / 1000.0, 3) if ef is not None else None
    except (TypeError, ValueError):
        co2 = None
    cost = None
    cost_note = None
    tariff_missing = tariff is None
    if tariff is not None:
        try:
            cost = round(float(saved) * float(tariff), 2)
        except (TypeError, ValueError):
            cost = None
            cost_note = '电价无法解析'
    else:
        cost_note = '节电和减碳已经算出来了。补充每度电的费用后，还能看看一年省多少钱。'
    out['co2_reduction_t'] = co2
    out['annual_cost_saving_cny'] = cost
    out['cost_note'] = cost_note
    out['tariff_missing'] = tariff_missing
    out['tariff_is_reference'] = bool(tariff_is_reference) if cost is not None else False
    return out


def _run_professional_measure(measure, *, ef, tariff, tariff_is_reference: bool) -> dict:
    """对专业措施调用同一物理引擎。"""
    method = getattr(measure, 'method_code', None) or getattr(measure, 'calculation_method', None)
    inputs = dict(getattr(measure, 'model_inputs', None) or {})
    raw = phys.run_method(method, inputs)
    return _enrich_phys_with_carbon_cost(
        raw, ef=ef, tariff=tariff, tariff_is_reference=tariff_is_reference,
    )


def _build_selected_assessment(
    case: CaseState,
    *,
    mill_result: dict,
    pump_result: dict,
    mill_in: dict,
    tariff,
    tariff_is_reference: bool,
    tariff_source,
    factor_meta: dict,
    ef,
) -> dict:
    """统一主结果：首页/收益页/报告只认此对象，禁止选泵却显示球磨数字。"""
    measure = _resolve_selected_measure(case)
    sc = case.scenario
    tech = _technology_type(measure, case)
    calc_method = _calculation_method(measure, case)
    device = _device_by_id(case, measure.device_id) if measure else None
    device_id = measure.device_id if measure else None
    device_name = (device.name if device else None) or (measure.target_device if measure else None)
    measure_id = measure.id if measure else sc.selected_measure_id
    measure_name = measure.name if measure else None
    use_fraction = tech in ('pump', 'generic_fraction')

    # 投资：只认本措施；显式绑定到本措施的情景投资也可
    inv = None
    if measure is not None:
        if measure.investment_cny is not None:
            inv = measure.investment_cny
        else:
            bind_id = getattr(sc, 'investment_bound_measure_id', None)
            if bind_id == measure.id and sc.investment_cny is not None:
                inv = sc.investment_cny
    else:
        inv = sc.investment_cny

    om, om_label = _resolve_annual_om(case, measure)
    om_for_net = 0.0 if om is None else float(om)

    def _payback_from(annual_cost):
        if annual_cost is None:
            return None, '节电和减碳已经算出来了。补充每度电的费用后，还能看看一年省多少钱。', 'partial'
        if om is None:
            return None, '改造后每年可能增加维护费用。填写预计金额，或明确选择暂按0元试算。', 'partial'
        net = round(annual_cost - om_for_net, 2)
        if inv is None:
            return None, '这项改造要花多少钱还没确定，先不计算回本时间。', 'partial'
        try:
            if float(inv) < 0:
                return None, '投资金额不能小于0，不能给出确定回本结论。', 'partial'
        except (TypeError, ValueError):
            return None, '投资金额无效，不能给出确定回本结论。', 'partial'
        if om is not None:
            try:
                if float(om) < 0:
                    return None, '年新增维护费不能小于0，不能给出确定回本结论。', 'partial'
            except (TypeError, ValueError):
                pass
        if net <= 0:
            return None, '当前假设下无法依靠节能回本', 'partial'
        return round(inv / net, 2), None, 'ok'

    waiting = None
    phys_out: dict = {}
    incompatible_method = False
    if measure is not None and device is not None and calc_method and calc_method != 'unsupported':
        if not equip_cat.method_compatible_with_device(device, calc_method):
            incompatible_method = True
            waiting = '设备类型已更改。原计算方法不再适用，请重新选择测算方法。'
    if incompatible_method:
        energy = {
            'saved_kwh': None, 'status': 'insufficient_data',
            'message': waiting, 'formula': None,
        }
        carbon = {'co2_reduction_t': None, 'status': 'insufficient_data', 'message': waiting}
        cost = {
            'annual_cost_saving_cny': None, 'annual_net_saving_cny': None,
            'status': 'insufficient_data', 'cost_note': waiting,
            'tariff_missing': True, 'tariff_is_reference': False,
        }
        pb, pb_msg, pb_st = None, waiting, 'insufficient_data'
    elif tech.startswith('professional_') and measure is not None:
        src = _run_professional_measure(
            measure, ef=ef, tariff=tariff, tariff_is_reference=tariff_is_reference,
        )
        if src.get('status') != 'ok':
            waiting = src.get('error') or src.get('message') or '专业模型条件不足或输入无效'
            energy = {
                'saved_kwh': None, 'status': 'insufficient_data',
                'message': waiting, 'formula': None,
            }
            carbon = {'co2_reduction_t': None, 'status': 'insufficient_data', 'message': waiting}
            cost = {
                'annual_cost_saving_cny': None, 'annual_net_saving_cny': None,
                'status': 'insufficient_data', 'cost_note': waiting,
                'tariff_missing': True, 'tariff_is_reference': False,
            }
            pb, pb_msg, pb_st = None, waiting, 'insufficient_data'
        else:
            energy = {
                'saved_kwh': src.get('saved_kwh'), 'status': 'ok',
                'message': None, 'formula': src.get('formula'),
            }
            carbon = {'co2_reduction_t': src.get('co2_reduction_t'), 'status': 'ok', 'message': None}
            annual_cost = src.get('annual_cost_saving_cny')
            net = None if annual_cost is None or om is None else round(annual_cost - om_for_net, 2)
            pb, pb_msg, pb_st = _payback_from(annual_cost)
            cost = {
                'annual_cost_saving_cny': annual_cost,
                'annual_net_saving_cny': net,
                'status': 'ok' if annual_cost is not None else 'partial',
                'cost_note': src.get('cost_note'),
                'tariff_missing': src.get('tariff_missing'),
                'tariff_is_reference': src.get('tariff_is_reference'),
            }
            # 透传假设（如未核验安全风量）
            if src.get('assumptions'):
                energy['assumptions'] = src.get('assumptions')
            if src.get('safety_conflict'):
                waiting = src.get('error') or '安全条件冲突，禁止推荐'
            phys_out = src
    elif tech == 'unsupported':
        waiting = '目前还没有适用计算模型'
        energy = {
            'saved_kwh': None, 'status': 'insufficient_data',
            'message': waiting, 'formula': None,
        }
        carbon = {'co2_reduction_t': None, 'status': 'insufficient_data', 'message': waiting}
        cost = {
            'annual_cost_saving_cny': None, 'annual_net_saving_cny': None,
            'status': 'insufficient_data', 'cost_note': waiting,
            'tariff_missing': True, 'tariff_is_reference': False,
        }
        pb, pb_msg, pb_st = None, waiting, 'insufficient_data'
    elif use_fraction:
        # 对选中比例措施独立核算（泵或通用）；不借用旁路第一台泵
        frac_i, base_i, src_id, frac_err = _fraction_inputs_for_measure(case, measure)
        if tech == 'pump' and (frac_i is None or base_i is None):
            # 回退到已算好的 pump_result（含示范公开比例）
            src = pump_result or {}
        else:
            if frac_i is None or base_i is None:
                waiting = frac_err or (
                    '请填写这台设备改造前一年的用电量，才能按节电比例估算。'
                    if base_i is None else '缺少节电比例'
                )
                if tech == 'generic_fraction' and frac_err:
                    waiting = frac_err
                src = {
                    'status': 'insufficient_data',
                    'saved_kwh': None,
                    'message': waiting,
                    'error': waiting,
                }
            else:
                src = pump_scenario(
                    pump_baseline_kwh=base_i,
                    saving_fraction=frac_i,
                    ef_kgco2_per_kwh=ef,
                    tariff_cny_per_kwh=tariff,
                    tariff_is_reference=tariff_is_reference,
                )
                if src.get('status') == 'ok' and tech == 'generic_fraction':
                    src['formula'] = (
                        f'通用比例情景：节电 = 基准年电耗 {base_i} × 节电率 {frac_i}'
                    )
                    src['model_label'] = '通用比例情景'
        if src.get('status') != 'ok':
            waiting = src.get('message') or src.get('error') or (
                '请填写这台水泵改造前一年的用电量，才能算它能省多少电。' if tech == 'pump'
                else '比例节电测算条件不足'
            )
            if tech == 'pump':
                waiting = _human_waiting('pump', pump_baseline_missing=True) or waiting
            energy = {
                'saved_kwh': None, 'status': 'insufficient_data',
                'message': waiting, 'formula': None,
            }
            carbon = {'co2_reduction_t': None, 'status': 'insufficient_data', 'message': waiting}
            cost = {
                'annual_cost_saving_cny': None, 'annual_net_saving_cny': None,
                'status': 'insufficient_data', 'cost_note': waiting,
                'tariff_missing': True, 'tariff_is_reference': False,
            }
            pb, pb_msg, pb_st = None, waiting, 'insufficient_data'
        else:
            energy = {
                'saved_kwh': src.get('saved_kwh'), 'status': 'ok',
                'message': None, 'formula': src.get('formula'),
            }
            carbon = {'co2_reduction_t': src.get('co2_reduction_t'), 'status': 'ok', 'message': None}
            annual_cost = src.get('annual_cost_saving_cny')
            net = None if annual_cost is None or om is None else round(annual_cost - om_for_net, 2)
            pb, pb_msg, pb_st = _payback_from(annual_cost)
            cost = {
                'annual_cost_saving_cny': annual_cost,
                'annual_net_saving_cny': net,
                'status': 'ok' if annual_cost is not None else 'partial',
                'cost_note': src.get('cost_note'),
                'tariff_missing': src.get('tariff_missing'),
                'tariff_is_reference': src.get('tariff_is_reference'),
            }
    else:
        src = mill_result or {}
        if src.get('status') != 'ok':
            if mill_in.get('power_drop_kw') is None:
                waiting = '还差功率降幅。填写改造后预计少用多少功率后再计算。'
            elif mill_in.get('hours') is None:
                waiting = '还差运行时间。填入预计每年能节省电的小时数后再计算。'
            else:
                waiting = src.get('error') or src.get('message') or '节电测算条件不足'
            energy = {
                'saved_kwh': None, 'status': 'insufficient_data',
                'message': waiting, 'formula': None,
            }
            carbon = {'co2_reduction_t': None, 'status': 'insufficient_data', 'message': waiting}
            cost = {
                'annual_cost_saving_cny': None, 'annual_net_saving_cny': None,
                'status': 'insufficient_data', 'cost_note': waiting,
                'tariff_missing': True, 'tariff_is_reference': False,
            }
            pb, pb_msg, pb_st = None, waiting, 'insufficient_data'
        else:
            energy = {
                'saved_kwh': src.get('saved_kwh'), 'status': 'ok',
                'message': None, 'formula': src.get('formula'),
            }
            carbon = {'co2_reduction_t': src.get('co2_reduction_t'), 'status': 'ok', 'message': None}
            annual_cost = src.get('annual_cost_saving_cny')
            net = None if annual_cost is None or om is None else round(annual_cost - om_for_net, 2)
            pb, pb_msg, pb_st = _payback_from(annual_cost)
            cost = {
                'annual_cost_saving_cny': annual_cost,
                'annual_net_saving_cny': net,
                'status': 'ok' if annual_cost is not None else 'partial',
                'cost_note': src.get('cost_note'),
                'tariff_missing': src.get('tariff_missing'),
                'tariff_is_reference': src.get('tariff_is_reference'),
            }

    payback = {
        'simple_payback_years': pb,
        'payback_message': pb_msg,
        'status': pb_st,
    }
    source_trace = {
        'tariff': {'value': tariff, 'is_reference': tariff_is_reference, 'source': tariff_source},
        'factor_meta': factor_meta,
        'investment_cny': inv,
        'investment_bound_measure_id': getattr(sc, 'investment_bound_measure_id', None),
        'mode': 'pending',
        'emission_factor': ef,
    }
    # 通用比例模型标注
    cat = _device_category(device)
    model_label = None
    if tech == 'generic_fraction':
        # V8：按设备族标注，避免空压机等被误认为已有专业模型
        cat_labels = {
            'compressor': '通用比例情景（空压机·非专业工况模型）',
            'fan': '通用比例情景（风机）',
            'crusher': '通用比例情景（破碎）',
            'conveyor': '通用比例情景（输送）',
            'motor': '通用比例情景（电机）',
            'sag': '通用比例情景（半自磨）',
            'transformer': '通用比例情景（变压器）',
            'mill': '通用比例情景（磨机）',
        }
        model_label = cat_labels.get(cat, '通用比例情景')
    elif tech == 'pump':
        model_label = '水泵比例节电模型'
    elif tech in ('process', 'vfd'):
        model_label = '按功率变化估算'
    elif tech.startswith('professional_'):
        model_label = (equip_cat.METHOD_META.get(calc_method) or {}).get('label_zh') or (
            equip_cat.METHOD_META.get(calc_method) or {}
        ).get('label') or calc_method

    source_trace['calculation_method'] = calc_method
    source_trace['model_label'] = model_label
    source_trace['annual_maintenance_cny'] = om
    source_trace['annual_maintenance_label'] = om_label
    source_trace['input_version'] = getattr(measure, 'revision', None) if measure else None
    source_trace['lifecycle_status'] = getattr(measure, 'lifecycle_status', None) if measure else None
    if measure is not None:
        source_trace['power_drop_kw'] = measure.power_drop_kw
        source_trace['saving_fraction'] = measure.saving_fraction
    if tech.startswith('professional_'):
        source_trace['mode'] = 'professional'
        source_trace['model_inputs'] = dict(getattr(measure, 'model_inputs', None) or {}) if measure else {}
    else:
        source_trace['mode'] = (
            'pump' if tech == 'pump'
            else ('generic_fraction' if tech == 'generic_fraction' else 'mill')
        )

    assumptions = list(energy.get('assumptions') or [])
    if calc_method == 'power_drop_hours' and device is not None and _device_category(device) == 'pump':
        if MEASURED_POWER_DROP_NOTE not in assumptions:
            assumptions.append(MEASURED_POWER_DROP_NOTE)
        model_label = '通用实测功率差情景（水泵）'
    energy_saved = energy.get('saved_kwh')
    energy_increase = energy_saved is not None and float(energy_saved) < 0
    recommendable = energy_saved is not None and energy.get('status') == 'ok' and not energy_increase
    block_reason = None
    hours_conflict_flag = False
    hours_conflict_msg = None
    safety_unverified = False
    # 专业模型小时不得大于同设备台账小时
    mh = _professional_hours(measure)
    if mh is not None and device is not None and getattr(device, 'hours', None) is not None:
        if float(mh) > float(device.hours):
            hours_conflict_flag = True
            hours_conflict_msg = _hours_conflict_message(mh, device.hours)
            recommendable = False
            block_reason = hours_conflict_msg
            if not waiting:
                waiting = hours_conflict_msg
    # 矿井主扇安全风量未核：可看节能情景，不可经济推荐
    if _fan_safety_unverified(measure, device):
        safety_unverified = True
        recommendable = False
        block_reason = block_reason or SAFETY_UNVERIFIED_MSG
        if SAFETY_UNVERIFIED_MSG not in assumptions:
            assumptions.append(SAFETY_UNVERIFIED_MSG)
    mismatch = _calc_vs_nameplate_warning(measure, device, phys_out)
    if mismatch:
        recommendable = False
        block_reason = block_reason or mismatch
        if mismatch not in assumptions:
            assumptions.append(mismatch)
    before_input_power_kw = _extract_before_input_power(phys_out, measure)
    if energy_increase:
        recommendable = False
        block_reason = block_reason or '预计增加用电，不作节能技改优选'
    if any('安全风量下限' in str(a) or '最低安全风量' in str(a) for a in assumptions):
        safety_unverified = True
        recommendable = False
        block_reason = block_reason or SAFETY_UNVERIFIED_MSG
    if waiting and energy.get('saved_kwh') is None:
        recommendable = False
        block_reason = waiting
    if measure is not None and getattr(measure, 'lifecycle_status', None) in ('draft', 'invalid'):
        recommendable = False
        can_rank_economic = False
        block_reason = block_reason or '该措施为草稿或无效状态，不参与推荐和正式报告。'
    unbound_msg = '尚未绑定具体设备，缺乏设备基线及技术约束，不得视为可实施推荐'
    if measure is not None and not getattr(measure, 'device_id', None):
        recommendable = False
        block_reason = block_reason or unbound_msg
        if unbound_msg not in assumptions:
            assumptions.append(unbound_msg)
    elif measure is not None and getattr(measure, 'device_id', None) and device is None:
        missing_dev = f'措施绑定的设备 {measure.device_id} 不存在，不得视为可实施推荐'
        recommendable = False
        block_reason = block_reason or missing_dev
        if missing_dev not in assumptions:
            assumptions.append(missing_dev)
    baseline_exceed_msg = None
    if (
        calc_method == 'power_drop_hours'
        and energy_saved is not None
        and device is not None
        and getattr(device, 'baseline_kwh', None) is not None
    ):
        base_kwh = float(device.baseline_kwh)
        sk = float(energy_saved)
        if sk > base_kwh + 1e-6:
            baseline_exceed_msg = (
                f'理论节电 {sk:.0f} kWh 超过该设备同一核算期间登记的基准电耗 '
                f'{base_kwh:.0f} kWh，边界未确认，停止确定性投资推荐；'
                f'理论节电量仅作标注试算，未改写公式结果。'
            )
            recommendable = False
            block_reason = block_reason or baseline_exceed_msg
            if baseline_exceed_msg not in assumptions:
                assumptions.append(baseline_exceed_msg)
    display_energy_label = '预计增加用电' if energy_increase else '预计节电'
    display_carbon_label = '预计增加相关排放' if energy_increase else '预计减排'
    can_rank_economic = bool(recommendable) and not safety_unverified and not hours_conflict_flag and not energy_increase

    return {
        'device_id': device_id,
        'device_name': device_name,
        'measure_id': measure_id,
        'measure_name': measure_name,
        'technology_type': tech,
        'calculation_method': calc_method,
        'model_label': model_label,
        'annual_maintenance_cny': om,
        'annual_maintenance_label': om_label,
        'energy': energy,
        'carbon': carbon,
        'cost': cost,
        'payback': payback,
        'investment_cny': inv,
        'source_trace': source_trace,
        'waiting_message': waiting if energy.get('saved_kwh') is None else None,
        'status': energy.get('status'),
        'saved_kwh': energy.get('saved_kwh'),
        'co2_reduction_t': carbon.get('co2_reduction_t'),
        'annual_cost_saving_cny': cost.get('annual_cost_saving_cny'),
        'annual_net_saving_cny': cost.get('annual_net_saving_cny'),
        'simple_payback_years': payback.get('simple_payback_years'),
        'payback_message': payback.get('payback_message'),
        'cost_note': cost.get('cost_note'),
        'tariff_is_reference': cost.get('tariff_is_reference'),
        'tariff_missing': cost.get('tariff_missing'),
        'formula': energy.get('formula'),
        'assumptions': assumptions,
        'recommendable': recommendable,
        'recommendation_blocked': bool(block_reason) and not recommendable,
        'recommendation_block_reason': block_reason,
        'safety_note': next((a for a in assumptions if '安全' in str(a)), SAFETY_UNVERIFIED_MSG if safety_unverified else None),
        'safety_unverified': safety_unverified,
        'hours_conflict': hours_conflict_flag,
        'hours_conflict_message': hours_conflict_msg,
        'can_rank_economic': can_rank_economic,
        'energy_direction': 'increase' if energy_increase else 'save',
        'display_energy_label': display_energy_label,
        'display_carbon_label': display_carbon_label,
        'increased_kwh': round(abs(float(energy_saved)), 2) if energy_increase else None,
        'before_input_power_kw': round(before_input_power_kw, 6) if before_input_power_kw is not None else None,
        'consistency_warnings': ([mismatch] if mismatch else []) + ([baseline_exceed_msg] if baseline_exceed_msg else []) + ([unbound_msg] if (measure is not None and not getattr(measure, 'device_id', None)) else []),
    }



def _mill_inputs_from_case(case: CaseState) -> dict:
    """按选中措施/设备解析球磨输入；新企业缺参时不套金徽默认。"""
    sc = case.scenario
    if _orphan_selected_measure(case):
        return {
            'error': (
                f'当前选中的措施编号 {sc.selected_measure_id} 已不在措施列表中，'
                f'请重新选择措施；不会沿用旧功率降幅继续计算'
            ),
            'power_drop_kw': None,
            'hours': sc.hours,
            'investment_cny': None,
            'measure': None,
            'orphan_selected': True,
            'consistency_warnings': [],
        }

    measure = _resolve_selected_measure(case)
    method = _calculation_method(measure, case)
    # 仅功率差法才用 power_drop；比例法/孤儿/未选不得沿用情景残留功率
    if measure is None:
        if case.is_demo or sc.use_jinhui_reference:
            power_drop = sc.power_drop_kw
        else:
            power_drop = None
    elif method == 'power_drop_hours':
        power_drop = measure.power_drop_kw if measure.power_drop_kw is not None else sc.power_drop_kw
    else:
        power_drop = None

    hours = sc.hours
    hours_source = 'scenario' if hours is not None else None
    consistency_warnings: list[str] = []
    investment = None
    if measure is not None:
        if measure.investment_cny is not None:
            investment = measure.investment_cny
        elif getattr(sc, 'investment_bound_measure_id', None) == measure.id:
            investment = sc.investment_cny
    else:
        investment = sc.investment_cny if (case.is_demo or sc.use_jinhui_reference) else None

    if measure is not None and measure.device_id:
        dev = _device_by_id(case, measure.device_id)
        if dev is None:
            return {
                'error': f'技改 {measure.id} 绑定的设备 {measure.device_id} 不存在',
                'power_drop_kw': power_drop,
                'hours': hours,
                'investment_cny': investment,
                'measure': measure,
                'consistency_warnings': consistency_warnings,
            }
        if hours is None and getattr(dev, 'hours', None) is not None:
            hours = dev.hours
            hours_source = 'device'
        # P0-05：功率降幅不得超过改造前平均运行功率（此处用设备功率近似）
        if (
            method == 'power_drop_hours'
            and power_drop is not None
            and dev.power_kw is not None
            and float(power_drop) > float(dev.power_kw)
        ):
            return {
                'error': (
                    f'功率降幅 {power_drop} kW 大于设备功率 {dev.power_kw} kW，'
                    f'物理上不成立；请区分额定功率与改造前平均运行功率'
                ),
                'power_drop_kw': power_drop,
                'hours': hours,
                'investment_cny': investment,
                'measure': measure,
                'consistency_warnings': consistency_warnings,
            }
        # P0-06：功率×小时 vs 基准年电耗
        if (
            dev.power_kw is not None and dev.hours is not None
            and getattr(dev, 'baseline_kwh', None) is not None
        ):
            cap = float(dev.power_kw) * float(dev.hours)
            if float(dev.baseline_kwh) > cap * 1.001:
                consistency_warnings.append(
                    f'设备「{dev.name}」功率×小时≈{cap:.0f} kWh，'
                    f'但基准年电耗={float(dev.baseline_kwh):.0f} kWh，存在不一致；'
                    f'禁止直接当作可信推荐'
                )
        # 水泵可用通用实测功率差情景（与目录声明一致），不再在此拒绝

    return {
        'error': None,
        'power_drop_kw': power_drop,
        'hours': hours,
        'hours_source': hours_source,
        'investment_cny': investment,
        'measure': measure,
        'consistency_warnings': consistency_warnings,
    }


def _baseline_for_fraction_measure(case: CaseState, measure) -> tuple[float | None, str | None]:
    """比例节电法：基准只认该措施绑定设备；情景 pump_baseline 仅在显式绑定到本措施/本设备时可用。
    禁止退回第一台同类设备。返回 (baseline, error)。
    """
    sc = case.scenario
    if measure is None:
        return None, '未指定措施'
    if measure.device_id:
        device = _device_by_id(case, measure.device_id)
        if device is None:
            return None, f'措施绑定的设备 {measure.device_id} 不存在或已删除，请重新绑定'
        if device.baseline_kwh is not None:
            return float(device.baseline_kwh), None
        # 情景级基准须绑定到本措施或本设备
        bound_m = getattr(sc, 'pump_baseline_bound_measure_id', None)
        bound_d = getattr(sc, 'pump_baseline_bound_device_id', None)
        if sc.pump_baseline_kwh is not None:
            if bound_m == measure.id or (bound_d is not None and bound_d == measure.device_id):
                return float(sc.pump_baseline_kwh), None
            # 兼容：尚未写绑定字段、但当前选中正是本措施时，允许使用刚填写的情景基准
            if bound_m is None and bound_d is None and sc.selected_measure_id == measure.id:
                return float(sc.pump_baseline_kwh), None
            # 示范旁路：金徽 rf-pump 且情景基准未绑到其他措施时，可供旁路 pump_scenario 使用
            if (
                (case.is_demo or sc.use_jinhui_reference)
                and measure.id == 'rf-pump'
                and (bound_m is None or bound_m == 'rf-pump')
            ):
                return float(sc.pump_baseline_kwh), None
        return None, ('请填写这台水泵改造前一年的用电量，才能算它能省多少电。'
                    if _device_category(device) == 'pump'
                    else '请填写这台设备改造前一年的用电量（基准年电耗）')
    # 无 device_id：仅示范兼容
    if case.is_demo or sc.use_jinhui_reference:
        if sc.pump_baseline_kwh is not None:
            return float(sc.pump_baseline_kwh), None
    return None, '措施未绑定设备，无法确定基准电耗'


def _fraction_inputs_for_measure(case: CaseState, measure) -> tuple[float | None, float | None, str | None, str | None]:
    """任意比例措施：(frac, baseline, src, error)。基准只认绑定设备。"""
    if measure is None:
        return None, None, None, '未指定措施'
    frac = measure.saving_fraction
    src = measure.id
    if frac is None:
        return None, None, src, '缺少节电比例'
    baseline, err = _baseline_for_fraction_measure(case, measure)
    return frac, baseline, src, err


def _pump_fraction_and_baseline(case: CaseState) -> tuple[float | None, float | None, str | None]:
    """旁路水泵模块：只解析绑定到泵设备的措施；选中非泵比例措施时不得污染本结果。
    若当前选中正是泵比例措施，则用该措施（及仅其设备基准）。
    """
    sc = case.scenario
    measure = _resolve_selected_measure(case)
    target = None
    if measure is not None and _is_pump_measure(measure, case):
        target = measure
    else:
        for rf in case.retrofits:
            if rf.saving_fraction is None:
                continue
            if _is_pump_measure(rf, case):
                target = rf
                break
        if target is None and (case.is_demo or sc.use_jinhui_reference):
            target = next((r for r in case.retrofits if r.id == 'rf-pump'), None)

    if target is None:
        return None, None, None

    frac = target.saving_fraction
    src = target.id
    if frac is None and (case.is_demo or sc.use_jinhui_reference):
        frac = PUBLISHED_PUMP_RATE
        src = 'published-jinhui'

    baseline, err = _baseline_for_fraction_measure(case, target)
    if err and baseline is None:
        return frac, None, src
    return frac, baseline, src


def _unit_product_kwh(case: CaseState) -> dict:
    """单位产品电耗：需有处理量。"""
    sc = case.scenario
    # 优先选中球磨设备
    measure = _resolve_selected_measure(case)
    dev = _device_by_id(case, measure.device_id) if measure else None
    if dev is None:
        mills = _devices_of_type(case, '球磨')
        dev = mills[0] if mills else None
    output = sc.output_t if sc.output_t is not None else (dev.output_t if dev else None)
    # 电耗来源：设备功率×小时 或 baseline
    kwh = None
    if dev and dev.power_kw is not None and dev.hours is not None:
        kwh = dev.power_kw * dev.hours
    elif dev and dev.baseline_kwh is not None:
        kwh = dev.baseline_kwh
    if output is None or output <= 0:
        return {
            'status': 'insufficient_data',
            'kwh_per_t': None,
            'message': '还需要填写设备处理量（产量 t）才能计算单位产品电耗',
        }
    if kwh is None:
        return {
            'status': 'insufficient_data',
            'kwh_per_t': None,
            'message': '还需要填写设备功率与小时或基准年电耗',
        }
    return {
        'status': 'ok',
        'kwh_per_t': round(kwh / output, 4),
        'inputs': {'kwh': kwh, 'output_t': output},
        'formula': 'kWh/t = 设备年电耗 / 处理量t',
        'data_type': '模型估算',
    }


def recalculate_case(case: CaseState) -> dict[str, Any]:
    grade = infer_data_grade(case)
    case.data_grade = grade
    case.data_prep = build_data_prep(case)

    features = {
        'annual_scenario': True,
        'device_savings': True,
        'hourly_analysis': grade == 'hourly',
        'fake_hourly_forbidden': grade != 'hourly',
        'mars_advanced': False,
    }

    sc = case.scenario
    baseline = _effective_baseline(sc)
    mill_in = _mill_inputs_from_case(case)
    selected_measure = mill_in.get('measure') or _resolve_selected_measure(case)
    selected_tech = _technology_type(selected_measure, case)

    # 新企业仅年度数据且未启用参考：球磨模块 insufficient
    mill_blocked = False
    mill_block_msg = None
    mill_invalid = False
    if mill_in.get('error'):
        mill_blocked = True
        mill_block_msg = mill_in['error']
    elif (
        not case.is_demo
        and not sc.use_jinhui_reference
        and mill_in.get('power_drop_kw') is None
    ):
        mill_blocked = True
        mill_block_msg = '缺少设备技改参数：请新增措施并绑定设备，或显式启用「金徽参考情景」'

    # P0-03：有效节能小时不得超过同设备可信实际运行小时
    hours_conflict = False
    hours_conflict_msg = None
    eff_hours = mill_in.get('hours')
    if (
        selected_measure is not None
        and selected_tech in ('process', 'vfd')
        and eff_hours is not None
        and selected_measure.device_id
    ):
        bound_dev = _device_by_id(case, selected_measure.device_id)
        if bound_dev is not None and bound_dev.hours is not None:
            if float(eff_hours) > float(bound_dev.hours):
                hours_conflict = True
                hours_conflict_msg = _hours_conflict_message()
                mill_blocked = True
                mill_invalid = True
                mill_block_msg = hours_conflict_msg
        # 年度上限
        if float(eff_hours) < 0:
            hours_conflict = True
            hours_conflict_msg = '有效节能小时不得为负'
            mill_blocked = True
            mill_invalid = True
            mill_block_msg = hours_conflict_msg
        else:
            # K24：若填写 8784，按闰年上限接受；否则非闰年 8760
            yrs = [y.year for y in case.energy_years] if case.energy_years else []
            cal_year = max(yrs) if yrs else (2024 if float(eff_hours) > 8760 else 2025)
            ymax = _max_hours_for_year(cal_year)
            if float(eff_hours) > 8760 and float(eff_hours) <= 8784:
                ymax = max(ymax, 8784)  # 允许闰年全年连续运行
            if float(eff_hours) > ymax:
                hours_conflict = True
                hours_conflict_msg = f'有效节能小时不可超过 {ymax}'
                mill_blocked = True
                mill_invalid = True
                mill_block_msg = hours_conflict_msg

    tariff = sc.energy_price
    tariff_is_reference = False
    tariff_source = None
    if tariff is None:
        # 仅从本企业能源表推算（有费用才算），不静默套金徽
        for y in sorted(case.energy_years, key=lambda x: x.year, reverse=True):
            if y.elec_10k_kwh and y.cost_10k_cny is not None and y.elec_10k_kwh != 0:
                tariff = y.cost_10k_cny / y.elec_10k_kwh
                tariff_source = f'企业能源表{y.year}推算'
                break
    # 示范默认或用户显式勾选参考电价/金徽参考情景，才可用金徽账面强度
    # 电价参考须显式 use_jinhui_tariff_reference；技改参考 use_jinhui_reference 不再偷偷带电价
    use_ref_tariff = bool(getattr(sc, 'use_jinhui_tariff_reference', False))
    if tariff is None and (case.is_demo or use_ref_tariff):
        tariff = COST_INTENSITY_CNY_PER_KWH
        tariff_is_reference = True
        tariff_source = '金徽2025账面电费强度（参考，并非本企业实际电价）'
    elif tariff is not None and (use_ref_tariff and not sc.energy_price_user_override and not case.is_demo):
        # 用户勾选参考且未自填时，标记参考
        if abs(float(tariff) - COST_INTENSITY_CNY_PER_KWH) < 1e-9:
            tariff_is_reference = True
            tariff_source = tariff_source or '金徽参考电价（显式选用）'
    elif case.is_demo and not sc.energy_price_user_override:
        tariff_is_reference = False  # 示范自身口径
        tariff_source = tariff_source or '金徽示范默认电价强度'

    ef = sc.emission_factor if sc.emission_factor is not None else DEFAULT_FACTOR
    factor_meta = factor_meta_for(ef, user_override=bool(sc.emission_factor_user_override))

    om_selected, om_selected_label = _resolve_annual_om(case, selected_measure)

    if mill_blocked:
        mill_result = {
            'status': 'invalid_input' if mill_invalid else 'insufficient_data',
            'saved_kwh': None,
            'before_kwh': None,
            'after_kwh': None,
            'co2_reduction_t': None,
            'annual_cost_saving_cny': None,
            'annual_net_saving_cny': None,
            'simple_payback_years': None,
            'payback_message': None,
            'error': mill_block_msg,
            'message': mill_block_msg,
            'is_actual_annual_savings': False,
        }
        mill_error = mill_block_msg
    else:
        mill_result = mill_scenario(
            delta_power_kw=mill_in['power_drop_kw'],
            hours=mill_in['hours'],
            baseline_power_kw=baseline,
            tariff_cny_per_kwh=tariff,
            ef_kgco2_per_kwh=ef,
            investment_cny=mill_in['investment_cny'],
            annual_extra_om_cny=0.0 if om_selected is None else om_selected,
            source_type='scenario',
            tariff_is_reference=tariff_is_reference,
        )
        mill_error = mill_result.get('error') if mill_result.get('status') != 'ok' else None

    if mill_result.get('status') == 'ok':
        mill_result['carbon'] = estimate_grid_co2_t(
            mill_result['saved_kwh'], ef, user_override=bool(sc.emission_factor_user_override)
        )
        mill_result['roi'] = {
            'simple_payback_years': mill_result.get('simple_payback_years'),
            'payback_message': mill_result.get('payback_message'),
            'annual_net_saving_cny': mill_result.get('annual_net_saving_cny'),
            'data_type': '模型估算',
        }
        # 参数溯源：逐字段
        power_src = {
            'value': mill_in['power_drop_kw'],
            'unit': 'kW',
            'status': 'reported' if case.is_demo else 'assumed',
            'data_type': '披露' if case.is_demo else '假设',
            'source_name': '金徽2024年报' if case.is_demo else '用户/措施输入',
            'label': '披露' if case.is_demo else '假设',
        }
        if mill_in.get('hours_source') == 'device':
            bound = None
            if selected_measure is not None and selected_measure.device_id:
                bound = _device_by_id(case, selected_measure.device_id)
            dev_name = getattr(bound, 'name', None) or '当前设备'
            hours_src = {
                'value': mill_in['hours'],
                'unit': 'h',
                'status': 'assumed',
                'data_type': '设备数据',
                'source_name': f'继承自设备「{dev_name}」运行小时',
                'label': '继承自设备数据',
            }
        else:
            hours_src = {
                'value': mill_in['hours'],
                'unit': 'h',
                'status': 'assumed',
                'data_type': '假设',
                'source_name': '情景假设',
                'label': '假设',
            }
        inv_src = {
            'value': mill_in['investment_cny'],
            'unit': '元',
            'status': 'assumed' if mill_in['investment_cny'] is not None else 'null',
            'data_type': '假设',
            'source_name': '情景假设（非企业披露）',
            'label': '假设' if mill_in['investment_cny'] is not None else '待填写',
        }
        if tariff is None:
            price_src = {
                'value': None,
                'unit': '元/kWh',
                'status': 'missing',
                'data_type': '缺失',
                'source_name': '未提供',
                'label': '待填写电价',
                'note': '补充电价后可估算节省费用',
            }
        elif sc.energy_price_user_override:
            price_src = {
                'value': tariff,
                'unit': '元/kWh',
                'status': 'measured',
                'data_type': '用户提供',
                'source_name': '用户设定',
                'label': '用户设定',
            }
        elif tariff_is_reference:
            price_src = {
                'value': tariff,
                'unit': '元/kWh',
                'status': 'reference',
                'data_type': '参考',
                'source_name': tariff_source or '金徽参考电价',
                'label': '参考价格、并非本企业实际电价',
            }
        else:
            price_src = {
                'value': tariff,
                'unit': '元/kWh',
                'status': 'reported' if case.is_demo else 'assumed',
                'data_type': '披露' if case.is_demo else '推算',
                'source_name': tariff_source or ('金徽2025账面强度' if case.is_demo else '企业能源表推算'),
                'label': '披露推算' if case.is_demo else '企业推算',
            }
        mill_result['trace'] = {
            'inputs': mill_result.get('inputs'),
            'formula': mill_result.get('formula'),
            'param_sources': {
                'power_drop_kw': power_src,
                'hours': hours_src,
                'baseline_power_kw': {
                    'value': baseline,
                    'unit': 'kW',
                    'status': 'assumed' if baseline is not None else 'null',
                    'data_type': '假设',
                    'source_name': '示范假设' if sc.use_demo_baseline else ('用户填写' if baseline is not None else '未提供'),
                    'label': '示范假设' if sc.use_demo_baseline else ('用户填写' if baseline is not None else '未提供'),
                },
                'energy_price': price_src,
                'emission_factor': factor_meta,
                'investment_cny': inv_src,
            },
            'units': {'kwh': 'kWh', 'co2': 'tCO2', 'cost': '元', 'payback': '年'},
            'boundary': '电力相关减排情景；公司总量≠单设备；不可直接作为碳核证',
            'data_types': {
                'kwh': '模型估算',
                'co2': '模型估算',
                'cost': '模型估算',
                'hours': '假设',
                'investment': '假设',
            },
            'selected_measure_id': (mill_in['measure'].id if mill_in.get('measure') else None),
        }
        # 覆盖 mill_scenario 内固定 factor_method 文案
        mill_result['factor_method'] = factor_meta.get('label') or factor_meta.get('source_name')

    # 水泵：按设备绑定
    frac, pump_base, frac_src = _pump_fraction_and_baseline(case)
    if frac is None and pump_base is not None:
        pump_result = {
            'status': 'insufficient_data',
            'saved_kwh': None,
            'co2_reduction_t': None,
            'annual_cost_saving_cny': None,
            'message': '水泵缺少已绑定该设备的节电比例措施',
            'error': '水泵缺少已绑定该设备的节电比例措施',
            'mode': 'pump',
        }
    else:
        # 非示范且无参考、又无泵措施比例时，不拿金徽 12.93% 冒充
        pump_frac = frac
        if pump_frac is None:
            if case.is_demo or sc.use_jinhui_reference:
                pump_frac = PUBLISHED_PUMP_RATE
            else:
                pump_frac = None
        if pump_frac is None:
            pump_result = {
                'status': 'insufficient_data',
                'saved_kwh': None,
                'co2_reduction_t': None,
                'annual_cost_saving_cny': None,
                'message': '水泵缺少已绑定该设备的节电比例措施',
                'error': '水泵缺少已绑定该设备的节电比例措施',
                'mode': 'pump',
            }
        else:
            pump_result = pump_scenario(
                pump_baseline_kwh=pump_base,
                saving_fraction=pump_frac,
                ef_kgco2_per_kwh=ef,
                tariff_cny_per_kwh=tariff,
                tariff_is_reference=tariff_is_reference,
            )
        if pump_result.get('status') == 'ok':
            pump_result['measure_id'] = frac_src
            pump_result['factor_method'] = factor_meta.get('label')
    pump_error = None
    if pump_result.get('status') != 'ok':
        pump_error = pump_result.get('message') or pump_result.get('error')

    enterprise = aggregate_enterprise_energy([d.model_dump() for d in case.devices])
    unit_product = _unit_product_kwh(case)

    energy_summary = []
    for y in case.energy_years:
        intensity = None
        if y.elec_10k_kwh and y.cost_10k_cny is not None and y.elec_10k_kwh != 0:
            intensity = round(y.cost_10k_cny / y.elec_10k_kwh, 6)
        energy_summary.append({
            **y.model_dump(),
            'intensity_cny_per_kwh': intensity,
            # 缺失费用不显示为 0
            'cost_display': None if y.cost_10k_cny is None else y.cost_10k_cny,
            'diesel_display': None if y.diesel_10k_l is None else y.diesel_10k_l,
        })

    # 小时方案对比：仅功率差法措施适用；水泵/通用比例不得夹球磨小时表（P0-05）
    schemes = []
    scheme_meta = {
        'case_id': case.case_id,
        'device_id': selected_measure.device_id if selected_measure else None,
        'measure_id': selected_measure.id if selected_measure else sc.selected_measure_id,
        'model_type': selected_tech,
        'calculation_method': _calculation_method(selected_measure, case),
        'applicable': selected_tech in ('process', 'vfd'),
    }
    if selected_tech in ('process', 'vfd'):
        for h in (2000, 4000, 6000):
            row = {
                'hours': h,
                'case_id': scheme_meta['case_id'],
                'device_id': scheme_meta['device_id'],
                'measure_id': scheme_meta['measure_id'],
                'model_type': scheme_meta['model_type'],
                'data_type': '模型估算',
                'source': '当前选中措施的小时情景对照',
            }
            if mill_blocked or mill_in.get('power_drop_kw') is None or mill_invalid:
                row.update({
                    'status': 'invalid_input' if mill_invalid else 'insufficient_data',
                    'annual_electricity_saved_kwh': None,
                    'scenario_grid_co2_saved_t': None,
                    'scenario_electricity_cost_saved_cny': None,
                    'simple_payback_years': None,
                    'payback_message': mill_block_msg,
                })
                schemes.append(row)
                continue
            # 对照小时亦不得超过设备实际小时（若已知）
            if (
                selected_measure and selected_measure.device_id
                and (bd := _device_by_id(case, selected_measure.device_id)) is not None
                and bd.hours is not None and h > float(bd.hours)
            ):
                inapplicable = '超过该设备可用运行小时，不适用'
                row.update({
                    'status': 'invalid_input',
                    'annual_electricity_saved_kwh': None,
                    'scenario_grid_co2_saved_t': None,
                    'scenario_electricity_cost_saved_cny': None,
                    'simple_payback_years': None,
                    'payback_message': inapplicable,
                    'error': inapplicable,
                    'inapplicable_reason': inapplicable,
                })
                schemes.append(row)
                continue
            r = mill_scenario(
                delta_power_kw=mill_in['power_drop_kw'],
                hours=h,
                baseline_power_kw=baseline,
                tariff_cny_per_kwh=tariff,
                ef_kgco2_per_kwh=ef,
                investment_cny=mill_in['investment_cny'],
                annual_extra_om_cny=0.0 if om_selected is None else om_selected,
                tariff_is_reference=tariff_is_reference,
            )
            row.update({
                'status': r.get('status'),
                'annual_electricity_saved_kwh': r.get('saved_kwh'),
                'scenario_grid_co2_saved_t': r.get('co2_reduction_t'),
                'scenario_electricity_cost_saved_cny': r.get('annual_cost_saving_cny'),
                'simple_payback_years': r.get('simple_payback_years'),
                'payback_message': r.get('payback_message'),
                'error': r.get('error'),
            })
            schemes.append(row)
    # 泵/通用比例：不生成球磨小时对照表

    # P1-03：比例/水泵/通用比例不得夹球磨 before/after
    if selected_tech in ('pump', 'generic_fraction'):
        frac_base, _ferr = _baseline_for_fraction_measure(case, selected_measure) if selected_measure else (None, None)
        saved_frac = (pump_result or {}).get('saved_kwh') if selected_tech == 'pump' else None
        # selected_assessment 稍后构建；此处用 pump_result / 比例结果
        if selected_tech == 'generic_fraction':
            # 从即将用于 selected 的同源：pump_scenario 也被复用算比例
            saved_frac = (pump_result or {}).get('saved_kwh')
        before_kwh = float(frac_base) if frac_base is not None else None
        after_kwh = (
            round(before_kwh - float(saved_frac), 2)
            if before_kwh is not None and saved_frac is not None else None
        )
        before_after = {
            'before_kwh': before_kwh,
            'after_kwh': after_kwh,
            'before_scenario_kwh': before_kwh,
            'after_scenario_kwh': after_kwh,
            'saved_kwh': saved_frac,
            'has_baseline': before_kwh is not None,
            'display_before': (
                f'{before_kwh:,.0f}' if before_kwh is not None else '尚无改造前基准电耗'
            ),
            'display_after': (
                f'{after_kwh:,.0f}' if after_kwh is not None else '待补基准或节电比例'
            ),
            'note': (
                '按设备改造前一年电耗 × 节电比例估算（非球磨功率差模型）'
                if before_kwh is not None
                else '请填写本设备改造前一年用电量（基准年电耗）'
            ),
            'baseline_power_kw': None,
            'baseline_status': 'null' if before_kwh is None else 'known',
            'data_type': '模型估算' if before_kwh is not None else None,
            'model': selected_tech,
        }
    else:
        before_kwh = mill_result.get('before_kwh')
        after_kwh = mill_result.get('after_kwh')
        before_after = {
            'before_kwh': before_kwh,
            'after_kwh': after_kwh,
            'before_scenario_kwh': before_kwh,
            'after_scenario_kwh': after_kwh,
            'saved_kwh': mill_result.get('saved_kwh'),
            'has_baseline': before_kwh is not None,
            'display_before': (
                f'{before_kwh:,.0f}' if before_kwh is not None else '尚无基准功率，无法确定'
            ),
            'display_after': (
                f'{after_kwh:,.0f}' if after_kwh is not None else '待输入'
            ),
            'note': (
                '已按基准功率计算改造前后绝对能耗（情景）'
                if before_kwh is not None
                else '想看改造前后能耗？请填写改造前平均功率，或启用 2,400 kW 示范假设'
            ),
            'baseline_power_kw': baseline,
            'baseline_status': (
                'assumed' if (sc.use_demo_baseline or baseline is not None) else 'null'
            ),
            'data_type': '模型估算' if before_kwh is not None else None,
            'model': selected_tech,
        }

    mars = {
        'enabled': False,
        'note': '高级研究：论文 MARS 模型需要 DCS 专用变量，本版不阻塞年度分析',
        'data_type': '文献',
    }
    try:
        mars['prediction_kw'] = mill_power_kw(EXAMPLE)
        mars['window'] = '2100–2380 kW'
        mars['example_only'] = True
    except Exception as e:
        mars['error'] = str(e)

    selected_assessment = _build_selected_assessment(
        case,
        mill_result=mill_result,
        pump_result=pump_result,
        mill_in=mill_in,
        tariff=tariff,
        tariff_is_reference=tariff_is_reference,
        tariff_source=tariff_source,
        factor_meta=factor_meta,
        ef=ef,
    )
    if isinstance(mill_result, dict) and mill_result.get('trace'):
        selected_assessment['trace'] = mill_result['trace']
        hours_src = (mill_result['trace'].get('param_sources') or {}).get('hours')
        if hours_src:
            selected_assessment.setdefault('source_trace', {})['hours'] = hours_src
    # P0-06：工况不一致时附加告警，禁止当作可信推荐
    cwarns = list(mill_in.get('consistency_warnings') or [])
    cwarns.extend(selected_assessment.get('consistency_warnings') or [])
    # 去重且保序
    seen = set()
    cwarns = [w for w in cwarns if not (w in seen or seen.add(w))]
    if cwarns:
        selected_assessment['consistency_warnings'] = cwarns
        selected_assessment['recommendable'] = False
        selected_assessment['recommendation_blocked'] = True
        selected_assessment['recommendation_block_reason'] = cwarns[0]
        if selected_assessment.get('waiting_message') is None:
            selected_assessment['waiting_message'] = cwarns[0]
        # 保留已算数值供排查，但状态降为 partial，前端不得直接推荐
        if selected_assessment.get('status') == 'ok':
            selected_assessment['status'] = 'partial'
    else:
        selected_assessment.setdefault('consistency_warnings', [])
        selected_assessment.setdefault('recommendable', selected_assessment.get('status') == 'ok')

    # 年新增运维未知：可算节电/减碳，但阻断确定回本与投资推荐
    if selected_assessment.get('annual_maintenance_cny') is None and (
        '未知' in (selected_assessment.get('annual_maintenance_label') or '')
    ):
        selected_assessment['recommendable'] = False
        selected_assessment['recommendation_blocked'] = True
        selected_assessment['recommendation_block_reason'] = (
            selected_assessment.get('payback_message')
            or '年新增运维未知，暂不给出确定回收期与投资推荐'
        )
        if selected_assessment.get('status') == 'ok':
            selected_assessment['status'] = 'partial'
        selected_assessment.setdefault('next_actions', [
            {'label': '填写年新增维护费', 'target': 'retrofit:maintenance'},
            {'label': '暂按0元试算（假设）', 'target': 'scenario:assume_zero_maintenance'},
        ])

    # V8 P0-07：同边界下设备节电不可显著超过企业年用电；不同边界只提示待确认
    boundary_flags: list[dict] = []
    try:
        saved = selected_assessment.get('saved_kwh')
        if saved is not None and case.energy_years:
            ey = sorted(case.energy_years, key=lambda x: x.year, reverse=True)[0]
            company_kwh = float(ey.elec_10k_kwh) * 10000.0
            device = None
            mid = selected_assessment.get('device_id') or sc.selected_measure_id
            if selected_assessment.get('device_id'):
                device = next((d for d in case.devices if d.id == selected_assessment.get('device_id')), None)
            ey_b = getattr(ey, 'boundary_id', None) or getattr(ey, 'site_id', None)
            d_b = (getattr(device, 'boundary_id', None) or getattr(device, 'site_id', None)) if device else None
            ey_scope = getattr(ey, 'measurement_scope', None) or 'enterprise_total'
            d_scope = (getattr(device, 'measurement_scope', None) or 'device') if device else 'device'
            if ey_b and d_b and str(ey_b) != str(d_b):
                msg = (
                    f'公司年用电（边界/站点 {ey_b}，范围 {ey_scope}）与设备用能'
                    f'（边界/站点 {d_b}，范围 {d_scope}）尚未匹配，不能直接相减或硬性冲突判定。'
                )
                boundary_flags.append({
                    'severity': 'pending_confirm',
                    'message': msg,
                    'company_year': ey.year,
                    'company_kwh': company_kwh,
                    'saved_kwh': float(saved),
                    'same_boundary': False,
                })
                selected_assessment['boundary_conflict'] = False
                selected_assessment['boundary_pending'] = True
                selected_assessment['boundary_message'] = msg
                selected_assessment.setdefault('consistency_warnings', []).append(msg)
            else:
                # 双方都未标边界：用户企业按同边界保守检查；示范案例也检查但文案标明情景
                same_boundary = True
                if company_kwh >= 0 and float(saved) > company_kwh * 1.001:
                    msg = (
                        f'设备预计节电 {float(saved):.0f} kWh 超过企业年用电 '
                        f'{company_kwh:.0f} kWh（{ey.year}年），存在边界/单位冲突'
                    )
                    boundary_flags.append({
                        'severity': 'critical',
                        'message': msg,
                        'company_year': ey.year,
                        'company_kwh': company_kwh,
                        'saved_kwh': float(saved),
                        'same_boundary': True,
                    })
                    selected_assessment['boundary_conflict'] = True
                    selected_assessment['boundary_pending'] = False
                    selected_assessment['boundary_message'] = msg
                    selected_assessment['recommendable'] = False
                    selected_assessment['recommendation_blocked'] = True
                    selected_assessment['recommendation_block_reason'] = msg
                    selected_assessment['status'] = 'partial' if selected_assessment.get('status') == 'ok' else selected_assessment.get('status')
                    selected_assessment.setdefault('consistency_warnings', []).append(msg)
    except Exception:
        pass

    summary = {
        'headline': case.name,

        'one_liner': '把企业能耗数据转化为可比较的减碳方案',
        'saved_kwh': selected_assessment.get('saved_kwh'),
        'co2_reduction_t': selected_assessment.get('co2_reduction_t'),
        'annual_cost_saving_cny': selected_assessment.get('annual_cost_saving_cny'),
        'simple_payback_years': selected_assessment.get('simple_payback_years'),
        'payback_message': selected_assessment.get('payback_message'),
        'status': selected_assessment.get('status'),
        'waiting_message': selected_assessment.get('waiting_message'),
        'measure_name': selected_assessment.get('measure_name'),
        'device_name': selected_assessment.get('device_name'),
        'technology_type': selected_assessment.get('technology_type'),
        'tags': {
            'power_drop': '公开资料' if case.is_demo else '项目参数',
            'hours': '情景估算',
            'factor': factor_meta.get('label') or '因子',
            'investment': '情景估算' if selected_assessment.get('investment_cny') is not None else '待填写',
        },
        'use_jinhui_reference': sc.use_jinhui_reference,
        'selected_measure_id': sc.selected_measure_id,
    }

    calculated_at = datetime.now().isoformat(timespec='seconds')
    measure_obj = mill_in.get('measure') or _resolve_selected_measure(case)
    measure_name = selected_assessment.get('measure_name')
    if measure_name is None and sc.selected_measure_id:
        m2 = next((r for r in case.retrofits if r.id == sc.selected_measure_id), None)
        measure_name = m2.name if m2 else None

    quote_choice = getattr(case, 'quote_choice', None)
    if quote_choice is None:
        quote_meta = None
    elif hasattr(quote_choice, 'model_dump'):
        quote_meta = quote_choice.model_dump()
    elif isinstance(quote_choice, dict):
        quote_meta = quote_choice
    else:
        quote_meta = None

    historical_quote = None
    if quote_meta and (quote_meta.get('superseded') or quote_meta.get('active') is False):
        historical_quote = dict(quote_meta)
        historical_quote['current'] = False
        historical_quote['status'] = 'historical'
        quote_meta = None  # 不再作为当前采用

    # 价格敏感性：不得写成确定回收期
    sensitivity = None
    if quote_meta and quote_meta.get('quote_usage_mode') == 'price_sensitivity':
        sens_inv = quote_meta.get('sensitivity_investment_cny') or quote_meta.get('total_quote_cny')
        annual_cost = selected_assessment.get('annual_cost_saving_cny')
        om = selected_assessment.get('annual_maintenance_cny')
        if om is None:
            om, _ = _resolve_annual_om(case, selected_measure)
        sens_pb = None
        om_note = '已知维护费'
        if om is None:
            om_note = '年新增运维费未知，未套用“运维费按0”的隐藏假设，故不提供数字回本期'
        else:
            om = float(om)
            if sens_inv is not None and annual_cost is not None:
                net = annual_cost - om
                if net > 0:
                    sens_pb = round(float(sens_inv) / net, 2)
        sensitivity = {
            'investment_cny': sens_inv,
            'hypothetical_payback_years': sens_pb,
            'note': '价格敏感性假设实验：非完整技改投资，不可作为确定回收期或采购推荐。' + (
                om_note if om is None else ''
            ),
            'maintenance_assumption': 'unknown_not_assumed_zero' if om is None else 'explicit_or_confirmed',
            'is_definitive_payback': False,
        }

    # V8 P1-02：快照 ID 对输入内容稳定；导出时刻单独记录 calculated_at
    import hashlib, json as _json
    _fp_payload = {
        'case_id': case.case_id,
        'case_name': case.name,
        'version_label': case.version_label,
        'selected_measure_id': selected_assessment.get('measure_id') or sc.selected_measure_id,
        'selected_measure_name': measure_name or selected_assessment.get('measure_name'),
        'selected_device_name': selected_assessment.get('device_name'),
        'device_names': [getattr(d, 'name', None) for d in (case.devices or [])],
        'measure_names': [getattr(r, 'name', None) for r in (case.retrofits or [])],
        'scenario': case.scenario.model_dump() if hasattr(case.scenario, 'model_dump') else dict(case.scenario),
        'saved_kwh': selected_assessment.get('saved_kwh'),
        'co2_reduction_t': selected_assessment.get('co2_reduction_t'),
        'investment_cny': selected_assessment.get('investment_cny'),
        'technology_type': selected_assessment.get('technology_type'),
        'origin_case_id': getattr(case, 'origin_case_id', None),
        'active_reference_keys': [
            f"{getattr(s, 'source_id', '')}:{getattr(s, 'parameter_key', '')}:{getattr(s, 'measure_id', '')}"
            for s in (case.reference_selections or [])
            if getattr(s, 'active', False) or getattr(s, 'applied', False)
        ],
        'model_version': 'v8-selected-assessment',
    }
    _fp = hashlib.sha256(
        _json.dumps(_fp_payload, sort_keys=True, ensure_ascii=False, default=str).encode('utf-8')
    ).hexdigest()[:16]
    assessment_snapshot = {
        'snapshot_id': f"snap-{case.case_id}-{_fp}",
        'version_hash': _fp,
        'case_id': case.case_id,
        'case_name': case.name,
        'is_demo': case.is_demo,
        'version_label': case.version_label,
        'calculated_at': calculated_at,
        'model_version': 'v8-selected-assessment',
        'selected_measure_id': selected_assessment.get('measure_id') or sc.selected_measure_id,
        'selected_measure_name': measure_name,
        'selected_device_id': selected_assessment.get('device_id'),
        'selected_device_name': selected_assessment.get('device_name'),
        'technology_type': selected_assessment.get('technology_type'),
        'selected_assessment': selected_assessment,
        'mill': {
            'saved_kwh': mill_result.get('saved_kwh'),
            'co2_reduction_t': mill_result.get('co2_reduction_t'),
            'annual_cost_saving_cny': mill_result.get('annual_cost_saving_cny'),
            'annual_net_saving_cny': mill_result.get('annual_net_saving_cny'),
            'simple_payback_years': mill_result.get('simple_payback_years'),
            'payback_message': mill_result.get('payback_message'),
            'status': mill_result.get('status'),
            'cost_note': mill_result.get('cost_note'),
            'tariff_missing': mill_result.get('tariff_missing'),
            'tariff_is_reference': mill_result.get('tariff_is_reference'),
        },
        'pump': {
            'saved_kwh': pump_result.get('saved_kwh'),
            'co2_reduction_t': pump_result.get('co2_reduction_t'),
            'annual_cost_saving_cny': pump_result.get('annual_cost_saving_cny'),
            'status': pump_result.get('status'),
            'message': pump_result.get('message'),
        },
        'factor_meta': factor_meta,
        'tariff': {
            'value': tariff,
            'is_reference': tariff_is_reference,
            'source': tariff_source,
            'user_override': bool(sc.energy_price_user_override),
        },
        'quote_choice': quote_meta,
        'historical_quote_choice': historical_quote,
        'price_sensitivity': sensitivity,
        'gaps': {
            'energy_price': tariff is None,
            'investment': selected_assessment.get('investment_cny') is None,
            'power_drop': (
                mill_in.get('power_drop_kw') is None
                if selected_assessment.get('technology_type') != 'pump' else False
            ),
            'hours': mill_in.get('hours') is None if selected_assessment.get('technology_type') != 'pump' else False,
            'pump_baseline': selected_assessment.get('technology_type') == 'pump' and selected_assessment.get('saved_kwh') is None,
        },
        'boundary': '设备级节电及相应电力相关排放变化；非完整企业碳盘查、非已实现减排核证',
        'needs_recalc': bool(getattr(case, 'needs_recalc', False)),
        'needs_recalc_message': '有未更新的修改，请先重新测算' if getattr(case, 'needs_recalc', False) else None,
    }
    if isinstance(mill_result, dict) and mill_result.get('status') == 'ok':
        mill_result['selected_measure_name'] = measure_name
        mill_result['tariff_source'] = tariff_source


    return {
        'case': case_public_dict(case),
        'features': features,
        'data_prep': [i.model_dump() for i in case.data_prep],
        'missing_fields': [],
        'energy_summary': energy_summary,
        'mill_scenario': mill_result,
        'mill_error': mill_error,
        'pump_scenario': pump_result,
        'pump_error': pump_error,
        'selected_assessment': selected_assessment,
        'consistency_warnings': list(mill_in.get('consistency_warnings') or []),
        'enterprise_devices': enterprise,
        'unit_product': unit_product,
        'schemes': schemes,
        'before_after': before_after,
        'mars_demo': mars,
        'summary': summary,
        'factor_meta': factor_meta,
        'calculated_at': calculated_at,
        'assessment_snapshot': assessment_snapshot,
        'boundary_flags': boundary_flags,
        'defaults': {
            'cost_intensity_reference': COST_INTENSITY_CNY_PER_KWH,
            'factor_meta': factor_meta,
            'demo_baseline_kw': DEMO_BASELINE_KW,
            'jinhui_tariff_reference': COST_INTENSITY_CNY_PER_KWH,
            'jinhui_power_drop_kw': 200.0,
            'jinhui_pump_rate': PUBLISHED_PUMP_RATE,
        },
    }


def full_recalculate() -> dict[str, Any]:
    return recalculate_case(get_case())


def compare_measures(case: CaseState, measure_a_id: str, measure_b_id: str, budget_cny: float | None = None) -> dict:
    """方案 A/B：不同技改项独立比较；投资不得跨措施继承。"""
    if measure_a_id == measure_b_id:
        raise ValueError('请选两项不同的改造措施，不能自己和自己比')
    if budget_cny is not None and budget_cny < 0:
        raise ValueError('预算不得为负')
    by_id = {r.id: r for r in case.retrofits}
    if measure_a_id not in by_id or measure_b_id not in by_id:
        raise KeyError('措施不存在')
    results = {}
    conflicts = []
    for key, mid in (('A', measure_a_id), ('B', measure_b_id)):
        c = case.model_copy(deep=True)
        c.scenario.selected_measure_id = mid
        m = by_id[mid]
        if m.power_drop_kw is not None:
            c.scenario.power_drop_kw = m.power_drop_kw
        # P0-02：措施无投资时清空情景投资，禁止继承另一措施的金额
        if m.investment_cny is not None:
            c.scenario.investment_cny = m.investment_cny
            if hasattr(c.scenario, 'investment_bound_measure_id'):
                c.scenario.investment_bound_measure_id = m.id
        else:
            c.scenario.investment_cny = None
            if hasattr(c.scenario, 'investment_bound_measure_id'):
                c.scenario.investment_bound_measure_id = None
        # 运维：只认本措施；禁止跨措施继承情景运维
        if m.annual_maintenance_cny is not None:
            c.scenario.annual_maintenance_cny = m.annual_maintenance_cny
            if hasattr(c.scenario, 'maintenance_bound_measure_id'):
                c.scenario.maintenance_bound_measure_id = m.id
        else:
            c.scenario.annual_maintenance_cny = None
            if hasattr(c.scenario, 'maintenance_bound_measure_id'):
                c.scenario.maintenance_bound_measure_id = m.id
        same = [r for r in case.retrofits if r.device_id and r.device_id == m.device_id and r.id != m.id]
        if same:
            conflicts.append({
                'measure_id': mid,
                'device_id': m.device_id,
                'others': [x.id for x in same],
                'message': '同一设备存在多项措施，比较时按单项独立情景，不可简单累加',
            })
        payload = recalculate_case(c)
        sa = payload.get('selected_assessment') or {}
        inv = sa.get('investment_cny')
        results[key] = {
            'measure_id': mid,
            'name': m.name,
            'device_id': m.device_id,
            'target_device': m.target_device,
            'investment_cny': inv,
            'investment_display': ('待补充' if inv is None else inv),
            'investment_missing': inv is None,
            'investment_status': 'pending' if inv is None else 'known',
            'saved_kwh': sa.get('saved_kwh'),
            'co2_reduction_t': sa.get('co2_reduction_t'),
            'annual_cost_saving_cny': sa.get('annual_cost_saving_cny'),
            'annual_net_saving_cny': sa.get('annual_net_saving_cny'),
            'annual_maintenance_cny': sa.get('annual_maintenance_cny'),
            'annual_maintenance_label': sa.get('annual_maintenance_label'),
            'boundary_conflict': bool(sa.get('boundary_conflict')),
            'boundary_pending': bool(sa.get('boundary_pending')),
            'boundary_message': sa.get('boundary_message'),
            'simple_payback_years': sa.get('simple_payback_years') if inv is not None else None,
            'payback_message': (
                '还不知道这项改造要花多少钱，暂时无法估算回本时间。'
                if inv is None
                else sa.get('payback_message')
            ),
            'status': sa.get('status'),
            'mode': sa.get('technology_type') or sa.get('source_trace', {}).get('mode'),
            'model_label': sa.get('model_label'),
            'waiting_message': sa.get('waiting_message'),
            'recommendable': sa.get('recommendable'),
            'safety_unverified': bool(sa.get('safety_unverified')),
            'hours_conflict': bool(sa.get('hours_conflict')),
            'hours_conflict_message': sa.get('hours_conflict_message'),
            'safety_note': sa.get('safety_note'),
            'can_rank_economic_item': sa.get('can_rank_economic'),
            'energy_direction': sa.get('energy_direction'),
            'display_energy_label': sa.get('display_energy_label'),
        }
        if budget_cny is not None and inv is not None:
            results[key]['within_budget'] = inv <= budget_cny
            results[key]['budget_status'] = 'within' if results[key]['within_budget'] else 'over'
            results[key]['budget_label'] = '在预算内' if results[key]['within_budget'] else '超预算'
        elif budget_cny is not None:
            results[key]['within_budget'] = 'unknown'
            results[key]['budget_status'] = 'unknown'
            results[key]['budget_label'] = '待补充'
            results[key]['budget_message'] = '无法判断是否符合预算（投资金额未知）'
        else:
            results[key]['within_budget'] = None
            results[key]['budget_status'] = None
            results[key]['budget_label'] = '待补充' if inv is None else None

    # V8 P0/08：经济推荐闸门——须双方均有可信电价/回本期、投资、净收益>0
    rec = None
    compare_only_reason = None
    comparison_mode = 'energy_carbon_only'
    can_rank_economic = False
    blocking_reasons: list[str] = []
    next_actions: list[dict] = []
    econ_cand = []

    for key, r in results.items():
        missing = []
        if r.get('saved_kwh') is None:
            missing.append('saved_kwh')
        if r.get('investment_cny') is None:
            missing.append('investment_cny')
        # 无回本期 ⇒ 通常缺电价、运维未知或净收益≤0
        if r.get('simple_payback_years') is None:
            if r.get('annual_cost_saving_cny') is None:
                missing.append('tariff_cny_per_kwh')
            om_v = r.get('annual_maintenance_cny')
            om_lbl = (r.get('annual_maintenance_label') or '')
            if om_v is None or '未知' in om_lbl:
                missing.append('annual_maintenance_cny')
            elif (r.get('annual_net_saving_cny') is not None) and float(r.get('annual_net_saving_cny') or 0) <= 0:
                missing.append('positive_net_saving')
            else:
                missing.append('payback_inputs')
        r['missing_inputs'] = missing
        r['payback_years'] = r.get('simple_payback_years')

    for key, r in results.items():
        if r.get('status') != 'ok':
            continue
        if r.get('investment_cny') is None:
            continue
        if r.get('simple_payback_years') is None:
            continue
        if budget_cny is not None:
            wb = r.get('within_budget')
            if wb is False or wb == 'unknown':
                continue
        econ_cand.append((key, r))

    both_have_investment = all(results[k].get('investment_cny') is not None for k in results)
    both_have_payback = all(results[k].get('simple_payback_years') is not None for k in results)
    any_missing_tariff = any('tariff_cny_per_kwh' in (results[k].get('missing_inputs') or []) for k in results)
    any_missing_inv = any(results[k].get('investment_cny') is None for k in results)

    if any_missing_tariff:
        blocking_reasons.append('尚未提供适用电价，暂不能比较回本时间')
        compare_only_reason = '两项改造的节电量已经算出来了，但我们还不知道每度电多少钱，暂时没法比较谁更划算。'
        next_actions = [
            {'label': '填写企业电价', 'target': 'carbon:tariff'},
            {'label': '选择参考电价', 'target': 'carbon:tariff-reference'},
        ]
    elif any_missing_inv:
        for key, r in results.items():
            if r.get('investment_cny') is None:
                blocking_reasons.append(f'方案{key}还没有改造费用')
                next_actions.append({'label': f'填写方案{key}投资', 'target': f'retrofit:investment:{key}'})
        next_actions.append({'label': '仅比较节电与减碳', 'target': 'schemes:energy_carbon'})
        compare_only_reason = '目前还不能判断哪项更划算。补充两项改造的投资后，可以进一步比较回本时间。'
    elif any('annual_maintenance_cny' in (results[k].get('missing_inputs') or []) for k in results):
        blocking_reasons.append('改造后每年可能增加维护费，尚未确认')
        compare_only_reason = '改造后每年可能增加维护费。填写金额，或明确选择暂按0元试算。'
        next_actions = [
            {'label': '填写年新增维护费', 'target': 'retrofit:maintenance'},
            {'label': '暂按0元试算（假设）', 'target': 'scenario:assume_zero_maintenance'},
        ]
    elif any(results[k].get('boundary_conflict') for k in results):
        blocking_reasons.append('存在同边界节电与企业用电冲突，暂不能做确定投资推荐')
        compare_only_reason = results['A'].get('boundary_message') or results['B'].get('boundary_message') or '边界冲突'
        next_actions = [
            {'label': '检查设备数据', 'target': 'devices:edit'},
            {'label': '查看计算依据', 'target': 'help:method'},
        ]
    elif not both_have_payback:
        # 净收益≤0 等
        for key, r in results.items():
            net = r.get('annual_net_saving_cny')
            if r.get('investment_cny') is not None and r.get('simple_payback_years') is None:
                if net is not None and float(net) <= 0:
                    r['payback_message'] = '在当前输入下，无法仅靠节能费用收回投资'
                    blocking_reasons.append(f'方案{key}年净收益≤0，无法仅靠节能费用回本')
        compare_only_reason = '当前条件下，无法形成可比的经济回收期排名。'
        next_actions = [
            {'label': '修改投入与运行条件', 'target': 'retrofit:inputs'},
            {'label': '查看节能减碳结果', 'target': 'schemes:energy_carbon'},
        ]

    # 仅当双方都有有限、非负回本期且无边界硬冲突时才允许经济推荐
    any_boundary_conflict = any(results[k].get('boundary_conflict') for k in results)
    any_safety_unverified = any(results[k].get('safety_unverified') for k in results)
    any_hours_conflict = any(results[k].get('hours_conflict') for k in results)
    any_not_recommendable = any(results[k].get('recommendable') is False for k in results)
    if any_safety_unverified:
        can_rank_economic = False
        comparison_mode = 'energy_carbon_only'
        rec = None
        blocking_reasons.append(SAFETY_UNVERIFIED_MSG)
        compare_only_reason = SAFETY_UNVERIFIED_MSG
    elif any_hours_conflict:
        can_rank_economic = False
        comparison_mode = 'energy_carbon_only'
        rec = None
        msg = results['A'].get('hours_conflict_message') or results['B'].get('hours_conflict_message') or _hours_conflict_message()
        blocking_reasons.append(msg)
        compare_only_reason = msg
    elif any_not_recommendable:
        can_rank_economic = False
        comparison_mode = 'energy_carbon_only'
        rec = None
        if compare_only_reason is None:
            compare_only_reason = '安全运行条件尚未核实或测算结论不可靠，暂不进行改造优选'
    elif both_have_investment and both_have_payback and len(econ_cand) >= 1 and not any_boundary_conflict:
        can_rank_economic = True
        comparison_mode = 'economic'
        def _score(item):
            r = item[1]
            pb = r.get('simple_payback_years')
            co2 = r.get('co2_reduction_t') or 0
            return (float(pb), -co2)
        econ_cand.sort(key=_score)
        best_key, best = econ_cand[0]
        if len(econ_cand) >= 2:
            reason = '在比较口径完整（电价、投资、正净收益）的方案中，按更短简单回收期排序；非投资建议'
        else:
            reason = '双方口径完整；在预算约束下仅此方案符合，供对照参考（非投资建议）'
        # 预算文案
        if budget_cny is not None:
            a_wb = results['A'].get('within_budget')
            b_wb = results['B'].get('within_budget')
            if a_wb is True and b_wb is False:
                reason = 'A在预算内，B超预算；在完整费用和可比条件下给出有限推荐'
            elif b_wb is True and a_wb is False:
                reason = 'B在预算内，A超预算；在完整费用和可比条件下给出有限推荐'
        rec = {
            'key': best_key,
            'choice': best_key,
            'measure_id': best['measure_id'],
            'name': best['name'],
            'reason': reason,
            'recommendation_type': 'economic',
            'sort_criterion': 'shortest_simple_payback_then_higher_co2',
        }
    else:
        rec = None
        comparison_mode = 'energy_carbon_only'
        can_rank_economic = False
        if compare_only_reason is None:
            compare_only_reason = '目前只能比较节电与减碳，不能做经济回收期排名。'

    # 单方有回本期、另一方缺失：可展示已具备计算，但不得宣称胜过对方
    if rec is None:
        have_pb = [k for k, r in results.items() if r.get('simple_payback_years') is not None]
        miss_pb = [k for k, r in results.items() if r.get('simple_payback_years') is None]
        if len(have_pb) == 1 and len(miss_pb) == 1:
            compare_only_reason = (
                f'方案{have_pb[0]}已具备回本测算，但方案{miss_pb[0]}条件不全；'
                f'不能宣布{have_pb[0]}已胜过{miss_pb[0]}。'
            )

    return {
        'A': results.get('A'),
        'B': results.get('B'),
        'scheme_A': results.get('A'),
        'scheme_B': results.get('B'),
        'recommendation': rec,
        'compare_only': rec is None,
        'compare_only_reason': compare_only_reason,
        'comparison_mode': comparison_mode,
        'can_rank_economic': can_rank_economic,
        'blocking_reasons': blocking_reasons,
        'next_actions': next_actions,
        'explanation': compare_only_reason,
        'conflicts': conflicts,
        'budget_cny': budget_cny,
    }



def build_report_text(payload: dict | None = None, *, sections: list[str] | None = None) -> str:
    """面向企业的评估报告 TXT；主结果只认 selected_assessment。"""
    data = payload or full_recalculate()
    case = data['case']
    sa = data.get('selected_assessment') or (data.get('assessment_snapshot') or {}).get('selected_assessment') or {}
    mill = data.get('mill_scenario') or {}
    ba = data.get('before_after') or {}
    snap = data.get('assessment_snapshot') or {}
    factor_meta = data.get('factor_meta') or factor_meta_for(
        case['scenario'].get('emission_factor') or DEFAULT_FACTOR,
        user_override=bool(case['scenario'].get('emission_factor_user_override')),
    )
    factor_label = factor_meta.get('label') or factor_meta.get('source_name') or ''
    ef_val = case['scenario'].get('emission_factor')
    measure_name = (
        sa.get('measure_name')
        or snap.get('selected_measure_name')
        or '（未指定措施名称）'
    )
    device_name = sa.get('device_name') or snap.get('selected_device_name') or ''
    identity = '公开历史案例' if case.get('is_demo') else '企业项目测算'
    if case['scenario'].get('use_jinhui_tariff_reference') and not case.get('is_demo'):
        identity = '参考价格情景'
    tariff_info = (sa.get('source_trace') or {}).get('tariff') or snap.get('tariff') or {}
    cost = sa.get('annual_cost_saving_cny')
    cost_line = (
        f"一年预计节约电费：{cost} 元"
        if cost is not None
        else (sa.get('cost_note') or '未填写电价，暂不估算电费变化。')
    )
    if sa.get('tariff_is_reference') and cost is not None:
        cost_line += '（参考价格、并非本企业实际电价）'
    payback = sa.get('simple_payback_years')
    payback_line = (
        f"预计回本时间：约 {payback} 年"
        if payback is not None
        else (sa.get('payback_message') or '投资或电价不足，暂不判断回本。')
    )
    # 价格敏感性不得写确定回收期
    sens = snap.get('price_sensitivity')
    if sens and payback is None and sens.get('hypothetical_payback_years') is not None:
        payback_line = (
            f"价格敏感性假设：若仅按该采购价 {sens.get('investment_cny')} 元试算，"
            f"粗算约 {sens.get('hypothetical_payback_years')} 年（非完整技改投资，不可作为确定回收期）"
        )

    wanted = set(sections or ['summary', 'energy', 'carbon', 'roi', 'methods', 'gaps', 'sources'])
    # P0-05：仅选 quotes 时不夹球磨节电/减排
    business_bits = {'summary', 'energy', 'carbon', 'roi', 'schemes'}
    include_main = bool(wanted & business_bits)

    lines = [
        '《设备节能与电力相关减排评估》',
        f'副标题：{identity}',
        '=' * 40,
        f"企业/案例：{case['name']}",
        f"版本：{case.get('version_label') or '当前版本'}",
        f"生成时间：{data.get('calculated_at', '')}（服务器本地时区）",
        '本报告用于节能改造前期比较；正式投资和碳排放申报前，需要用企业实测及有效合同进一步复核。',
    ]
    if snap.get('needs_recalc'):
        lines.append('提示：有未更新的修改，请先重新测算后再导出。')
    lines += [
        '',
        '这次算到了哪些部分？',
        '设备节电及相应电力排放变化（不是整厂碳核算，也不是已经核证的减排量）。',
        '',
    ]

    if include_main:
        lines += [
            '【本次测算结果】',
            f"设备：{device_name or '—'}",
            f"本次评估的改造：{measure_name}",
        ]
        # 节电数字与经济完整度解耦：运维未知时 status 可能是 partial，仍应写出已算节电/碳
        if sa.get('saved_kwh') is None:
            lines.append(f"说明：{sa.get('waiting_message') or sa.get('energy', {}).get('message') or '节电测算条件不足'}")
        else:
            if 'energy' in wanted or 'summary' in wanted:
                if sa.get('energy_direction') == 'increase':
                    lines.append(f"预计增加用电：{sa.get('increased_kwh') or abs(float(sa.get('saved_kwh')))} kWh")
                else:
                    lines.append(f"一年预计省电：{sa.get('saved_kwh')} kWh")
            if 'carbon' in wanted or 'summary' in wanted:
                if sa.get('co2_reduction_t') is None:
                    lines.append(f"电力相关减排：{sa.get('waiting_message') or '尚待补充'}")
                else:
                    if sa.get('energy_direction') == 'increase':
                        lines.append(
                            f"预计增加相关排放约 {abs(float(sa.get('co2_reduction_t')))} tCO₂"
                            f"（按 {factor_label or '所选因子'}，数值 {ef_val} kgCO₂/kWh）"
                        )
                    else:
                        lines.append(
                            f"预计减少与用电相关的排放约 {sa.get('co2_reduction_t')} tCO₂"
                            f"（按 {factor_label or '所选因子'}，数值 {ef_val} kgCO₂/kWh）"
                        )
            if 'roi' in wanted or 'summary' in wanted:
                lines.append(cost_line)
                lines.append(payback_line)
                if sa.get('status') == 'partial' and sa.get('payback_message'):
                    lines.append(f"经济完整度说明：{sa.get('payback_message')}")
                if tariff_info.get('value') is not None:
                    src = tariff_info.get('source') or ''
                    if tariff_info.get('is_reference'):
                        src = src or '金徽参考电价（试算）'
                    lines.append(f"所用电价：{tariff_info.get('value')} 元/kWh（{src}）")
                else:
                    lines.append('所用电价：尚未填写')
                inv = sa.get('investment_cny')
                lines.append(f"改造投资：{inv if inv is not None else '尚未填写'} 元")
            # 绝对前后能耗：功率差法展示功率基准；比例法展示电耗基准；不夹无关球磨值
            tech = sa.get('technology_type')
            if tech in ('process', 'vfd') and ba.get('model') in (None, 'process', 'vfd'):
                if ba.get('before_kwh') is not None:
                    lines.append(f"改造前全年用电（展示）：{ba.get('display_before')}")
                if ba.get('after_kwh') is not None:
                    lines.append(f"改造后全年用电（展示）：{ba.get('display_after')}")
            elif tech in ('pump', 'generic_fraction') and ba.get('before_kwh') is not None:
                lines.append(f"改造前基准电耗：{ba.get('display_before')} kWh")
                if ba.get('after_kwh') is not None:
                    lines.append(f"改造后估算电耗：{ba.get('display_after')} kWh")
            if sa.get('formula'):
                lines.append(f"计算方法：{sa.get('formula')}")
        lines.append('')

    if 'quotes' in wanted:
        qc = snap.get('quote_choice') or case.get('quote_choice')
        lines += ['【采购报价参考】']
        if qc and (qc.get('superseded') or qc.get('active') is False):
            lines.append('曾经引用·已失效，不再作为当前采用。')
            if qc.get('invalidated_reason'):
                lines.append(f"失效原因：{qc.get('invalidated_reason')}")
            if qc.get('invalidated_at'):
                lines.append(f"失效时间：{qc.get('invalidated_at')}")
            lines.append(f"历史记录：{qc.get('bidder_full_name') or qc.get('label') or '参考报价'}")
        elif not qc:
            hist = snap.get('historical_quote_choice')
            if hist:
                lines.append('曾经引用·已失效，不再作为当前采用。')
                if hist.get('invalidated_reason'):
                    lines.append(f"失效原因：{hist.get('invalidated_reason')}")
                if hist.get('invalidated_at'):
                    lines.append(f"失效时间：{hist.get('invalidated_at')}")
                lines.append(f"历史记录：{hist.get('bidder_full_name') or hist.get('label') or '参考报价'}")
            else:
                lines.append('尚未选择参考报价。')
        else:
            lines.append(f"选用：{qc.get('bidder_full_name') or qc.get('label') or '参考报价'}")
            if qc.get('project_title'):
                lines.append(f"项目：{qc.get('project_title')}")
            lines.append(f"报价金额：{qc.get('total_quote_cny')} 元")
            usage = qc.get('quote_usage_mode') or ''
            if usage == 'price_sensitivity':
                lines.append('用途：价格敏感性假设实验（不是完整技改投资）')
            elif usage:
                lines.append(f"用途：{usage}")
            compat = qc.get('compatibility_result') or '待核对型号与范围'
            # 禁止未核对却写已核实匹配
            if compat in ('已核实匹配', 'matched', 'verified'):
                if not qc.get('compatibility_verified_by_user'):
                    compat = '待核对型号与范围'
            lines.append(f"适配说明：{compat}")
            if qc.get('watermark'):
                lines.append(str(qc.get('watermark')))
            lines.append('说明：候选价不等于确认成交价；更换报价不会改变节电与减排结果。')
            if sens:
                lines.append(str(sens.get('note') or ''))
        lines.append('')

    if 'schemes' in wanted and include_main:
        tech = sa.get('technology_type')
        schemes_rows = data.get('schemes') or []
        # P0-05：水泵/通用比例不得附球磨小时对照；无适用表则明确说明
        if tech in ('pump', 'generic_fraction') or not schemes_rows:
            lines += [
                '【情景对照】',
                '当前选中措施不适用「小时对照」表（该表仅用于功率差×有效小时模型）。',
                f"设备ID：{sa.get('device_id') or '—'}；措施ID：{sa.get('measure_id') or '—'}；"
                f"模型：{sa.get('model_label') or tech or '—'}",
                '',
            ]
        else:
            lines += ['【小时情景对照】']
            lines.append(
                f"适用对象：case={case.get('case_id')} device={sa.get('device_id')} "
                f"measure={sa.get('measure_id')} model={sa.get('model_label') or tech}"
            )
            for s in schemes_rows:
                lines.append(_format_scheme_txt_line(s))
            lines.append('')

    # 仅当用户显式要 energy 且当前选中不是泵、又想看泵旁路时才附泵独立段——自选 quotes 不附
    if 'energy' in wanted and sa.get('technology_type') != 'pump' and 'pump_extra' in wanted:
        pump = data.get('pump_scenario') or {}
        lines += ['【水泵模块（独立）】', f"状态说明：{pump.get('message') or pump.get('status')}"]
        if pump.get('status') == 'ok':
            lines += [
                f"节电：{pump.get('saved_kwh')} kWh",
                f"电力相关减排：{pump.get('co2_reduction_t')} tCO₂",
            ]
        lines.append('')

    if 'methods' in wanted or 'gaps' in wanted:
        lines += ['【这次用了哪些假设？】']
        tech = sa.get('technology_type') or ''
        method = sa.get('calculation_method') or ''
        mi = (sa.get('source_trace') or {}).get('model_inputs') or {}
        if str(tech).startswith('professional_') or method in (
            'pump_hydraulic', 'fan_pressure_flow', 'motor_efficiency',
            'compressor_power_drop', 'transformer_losses',
        ):
            from . import equipment_catalog as _ec
            meta = _ec.METHOD_META.get(method) or {}
            filled = []
            for f in meta.get('fields') or []:
                key = f.get('key')
                val = mi.get(key) if isinstance(mi, dict) else None
                if val is None or val == '':
                    continue
                filled.append(f"- [已具备] {f.get('label') or key}: {val} {f.get('unit') or ''}".rstrip())
            lines.extend(filled or ['- 已按当前专业模型登记工况参数'])
            for a in (sa.get('assumptions') or []):
                kind = '风险提示' if any(k in str(a) for k in ('安全', '未核', '待核实')) else '假设'
                lines.append(f"- [{kind}] {a}")
        else:
            for item in data.get('data_prep') or []:
                label_txt = item.get('label') or ''
                if tech == 'pump' and any(k in label_txt for k in ('功率降幅', '球磨', '运行小时')):
                    continue
                if tech != 'pump' and '水泵基准' in label_txt:
                    continue
                st = item.get('status')
                label = {'ready': '已具备', 'assumed': '采用假设', 'needed': '可补充'}.get(st, st)
                lines.append(f"- [{label}] {label_txt}: {item.get('detail')}")
        if sa.get('safety_unverified') or sa.get('safety_note'):
            lines.append(f"- [风险提示] {sa.get('safety_note') or SAFETY_UNVERIFIED_MSG}")
        lines.append('')

    if 'sources' in wanted or 'methods' in wanted:
        from .report_sections import format_parameter_identity_lines
        lines += format_parameter_identity_lines(data, sa)
        lines += ['【相关研究资料】']
        for s in case.get('sources') or []:
            lines.append(f"- {s.get('name')} | {s.get('category')} | {s.get('url')}")
        # 历史/收藏资料不作为本次依据
        for sel in case.get('reference_selections') or []:
            if sel.get('active') or sel.get('applied'):
                continue
            title = sel.get('title') or sel.get('source_id') or '参考资料'
            status = '已撤销' if sel.get('revoked') else ('仅收藏' if sel.get('choice') == 'match_trial' or sel.get('apply_intent') == 'bookmark' else '历史记录')
            if sel.get('superseded'):
                status = '历史记录/已覆盖'
            lines.append(f"- [历史资料] {title} ｜ {status} ｜ 不等于本次采用的参数来源")
        lines += [
            '',
            '声明：节省电费不等于已经减碳；对外仅称「电力相关减排情景」。',
            '金徽公开示范只读；参考报价须用户显式勾选，禁止静默套用。',
            '结果是设备节电→电力相关减排→成本收益的情景估算，不是第三方核证减排量。',
        ]
    # 清理内部名泄漏
    text_out = '\n'.join(lines)
    for bad in ('None', 'null', 'AssessmentSnapshot', 'selected_assessment', 'rf-pump', 'rf-mill'):
        # 仅清理独立出现的内部 token；保留正常中文
        pass
    # 明确替换常见泄漏
    text_out = text_out.replace('AssessmentSnapshot', '本次测算')
    text_out = text_out.replace(' selected_assessment', '')
    # 措施 id 不以原始 id 展示：若误入则已用 measure_name
    return text_out



def sanitize_csv_cell(val: Any) -> str:
    """防止 CSV 公式注入（保留原文语义，供 csv.writer 再转义）。"""
    s = '' if val is None else str(val)
    # 前导空白后的公式字符同样危险
    stripped = s.lstrip(' \t')
    if stripped[:1] in ('=', '+', '-', '@'):
        return "'" + s
    if s[:1] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + s
    return s


def render_csv_rows(rows: list[list[Any]], *, with_bom: bool = True) -> str:
    """统一 CSV 序列化：正确引用逗号/引号/换行 + 公式注入防护。"""
    import csv
    import io
    buf = io.StringIO(newline='')
    w = csv.writer(buf, lineterminator='\n')
    for row in rows:
        w.writerow([sanitize_csv_cell(c) for c in row])
    text = buf.getvalue()
    if with_bom and not text.startswith('\ufeff'):
        text = '\ufeff' + text
    return text
