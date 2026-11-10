"""V8 设备类型与模型能力注册表。"""
from __future__ import annotations

from typing import Any

# capability tags (P1-08):
# - can_register: 可录入设备档案
# - can_trial_calc: 可独立试算
# - in_decision_flow: 已接入完整企业决策流程（可创建措施→节电→减排→报告）

EQUIPMENT_TYPES: list[dict[str, Any]] = [
    {
        'type_code': 'ball_mill',
        'display_name': '球磨机',
        'aliases': ['球磨机', '球磨'],
        'category': '磨矿与选矿',
        'model_status': 'supported',
        'supported_methods': ['power_drop_hours', 'baseline_fraction'],
        'can_register': True,
        'can_trial_calc': True,
        'in_decision_flow': True,
        'safety_notes': '',
    },
    {
        'type_code': 'sag_mill',
        'display_name': '半自磨机（SAG）',
        'aliases': ['半自磨机', '半自磨', 'SAG', 'sag'],
        'category': '磨矿与选矿',
        'model_status': 'reference_scenario',
        'supported_methods': ['power_drop_hours', 'baseline_fraction'],
        'can_register': True,
        'can_trial_calc': True,
        'in_decision_flow': True,  # 通用情景已接入
        'safety_notes': '仅额定功率时不可伪装实测实耗',
    },
    {
        'type_code': 'pump_drainage',
        'display_name': '矿井排水泵',
        'aliases': ['矿井排水泵', '排水泵', '主排水泵', '水泵'],
        'category': '供水、通风与压缩空气',
        'model_status': 'supported',
        'supported_methods': ['baseline_fraction', 'pump_hydraulic', 'power_drop_hours'],
        'can_register': True,
        'can_trial_calc': True,
        'in_decision_flow': True,
        'safety_notes': '主排水须保留安全备用能力',
    },
    {
        'type_code': 'pump_process',
        'display_name': '循环水泵/矿浆泵',
        'aliases': ['循环水泵', '矿浆泵', '尾矿泵', '工艺泵'],
        'category': '供水、通风与压缩空气',
        'model_status': 'supported',
        'supported_methods': ['baseline_fraction', 'pump_hydraulic'],
        'can_register': True,
        'can_trial_calc': True,
        'in_decision_flow': True,
        'safety_notes': '',
    },
    {
        'type_code': 'fan_ventilation',
        'display_name': '矿井通风机',
        'aliases': ['矿井通风机', '通风机', '主扇', '风机'],
        'category': '供水、通风与压缩空气',
        'model_status': 'supported',
        'supported_methods': ['power_drop_hours', 'baseline_fraction', 'fan_pressure_flow'],
        'can_register': True,
        'can_trial_calc': True,
        'in_decision_flow': True,
        'safety_notes': '风量不得低于安全下限；本平台不做自动控制建议',
    },
    {
        'type_code': 'compressor',
        'display_name': '空气压缩机',
        'aliases': ['空气压缩机', '空压机', '压缩机'],
        'category': '供水、通风与压缩空气',
        'model_status': 'reference_scenario',
        'supported_methods': ['baseline_fraction', 'power_drop_hours', 'compressor_power_drop'],
        'can_register': True,
        'can_trial_calc': True,
        'in_decision_flow': True,
        'safety_notes': '通用比例情景≠空压机专业气路模型；功率差为同任务参考情景',
    },
    {
        'type_code': 'crusher',
        'display_name': '破碎机',
        'aliases': ['破碎机', '破碎'],
        'category': '磨矿与选矿',
        'model_status': 'reference_scenario',
        'supported_methods': ['power_drop_hours', 'baseline_fraction'],
        'can_register': True,
        'can_trial_calc': True,
        'in_decision_flow': True,
        'safety_notes': '处理量不一致时须产量归一',
    },
    {
        'type_code': 'conveyor',
        'display_name': '带式输送机',
        'aliases': ['带式输送机', '皮带机', '输送机'],
        'category': '输送设备',
        'model_status': 'reference_scenario',
        'supported_methods': ['power_drop_hours', 'baseline_fraction'],
        'can_register': True,
        'can_trial_calc': True,
        'in_decision_flow': True,
        'safety_notes': '须说明可比输送任务',
    },
    {
        'type_code': 'motor_drive',
        'display_name': '高效电动机/驱动',
        'aliases': ['高效电动机', '电动机', '电机', '驱动'],
        'category': '电机与配电',
        'model_status': 'supported',
        'supported_methods': ['motor_efficiency', 'power_drop_hours'],
        'can_register': True,
        'can_trial_calc': True,
        'in_decision_flow': True,
        'safety_notes': '轴功率≠电机输入功率；勿与变频节电重复计量',
    },
    {
        'type_code': 'transformer',
        'display_name': '配电变压器',
        'aliases': ['配电变压器', '变压器'],
        'category': '电机与配电',
        'model_status': 'supported',
        'supported_methods': ['transformer_losses'],
        'can_register': True,
        'can_trial_calc': True,  # 可独立试算
        'in_decision_flow': True,  # V8：可创建专业措施
        'safety_notes': '损耗模型为简化估算，正式选型需现场负载实测',
    },
    {
        'type_code': 'generic',
        'display_name': '其他设备（仅录入/通用）',
        'aliases': ['其他设备', '通用', '其他'],
        'category': '其他设备',
        'model_status': 'reference_scenario',
        'supported_methods': ['baseline_fraction', 'power_drop_hours'],
        'can_register': True,
        'can_trial_calc': True,
        'in_decision_flow': True,
        'safety_notes': '仅通用情景，无专业工况模型',
    },
]

METHOD_META: dict[str, dict[str, Any]] = {
    'power_drop_hours': {
        'version': 'v8',
        'label': '按功率变化估算',
        'label_zh': '按功率变化估算',
        'required_inputs': ['power_drop_kw', 'hours'],
        'status': 'supported',
        'is_professional': False,
        'ui_hint': '只有设备用电量？也可以先做初步估算。',
    },
    'baseline_fraction': {
        'version': 'v8',
        'label': '按改造前用电量估算',
        'label_zh': '按改造前用电量估算',
        'required_inputs': ['baseline_kwh', 'saving_fraction'],
        'status': 'supported',
        'is_professional': False,
        'ui_hint': '只有设备用电量？也可以先做初步估算。',
    },
    'pump_hydraulic': {
        'version': 'v8',
        'label': '专业水力模型（需要工况数据）',
        'label_zh': '专业水力模型（需要工况数据）',
        'required_inputs': ['q_m3_h', 'head_m', 'eta_pump_before', 'eta_pump_after', 'hours'],
        'status': 'supported',
        'is_professional': True,
        'ui_hint': '有流量、扬程或效率记录？试试更贴近工况的测算。',
        'fields': [
            {'key': 'q_m3_h', 'label': '流量 Q', 'unit': 'm³/h', 'required': True},
            {'key': 'head_m', 'label': '扬程 H', 'unit': 'm', 'required': True},
            {'key': 'rho_kg_m3', 'label': '介质密度', 'unit': 'kg/m³', 'required': False, 'default': 1000},
            {'key': 'eta_pump_before', 'label': '改造前泵效率', 'unit': '0–1', 'required': True},
            {'key': 'eta_pump_after', 'label': '改造后泵效率', 'unit': '0–1', 'required': True},
            {'key': 'eta_motor', 'label': '电机效率', 'unit': '0–1', 'required': False, 'default': 0.90},
            {'key': 'eta_drive', 'label': '传动效率', 'unit': '0–1', 'required': False, 'default': 1.0},
            {'key': 'hours', 'label': '年有效小时', 'unit': 'h', 'required': True},
            {'key': 'accounting_year', 'label': '核算年度', 'unit': '年', 'required': False},
        ],
    },
    'fan_pressure_flow': {
        'version': 'v8',
        'label': '专业风量风压效率模型（需要工况数据）',
        'label_zh': '专业风量风压效率模型（需要工况数据）',
        'required_inputs': ['q_m3_s', 'delta_p_pa', 'eta_fan_before', 'eta_fan_after', 'hours'],
        'status': 'supported',
        'is_professional': True,
        'ui_hint': '有流量、扬程或效率记录？试试更贴近工况的测算。',
        'fields': [
            {'key': 'q_m3_s', 'label': '风量 Q', 'unit': 'm³/s', 'required': True},
            {'key': 'delta_p_pa', 'label': '风压差 Δp', 'unit': 'Pa', 'required': True},
            {'key': 'eta_fan_before', 'label': '改造前风机效率', 'unit': '0–1', 'required': True},
            {'key': 'eta_fan_after', 'label': '改造后风机效率', 'unit': '0–1', 'required': True},
            {'key': 'eta_motor', 'label': '电机效率', 'unit': '0–1', 'required': False, 'default': 0.90},
            {'key': 'eta_drive', 'label': '传动效率', 'unit': '0–1', 'required': False, 'default': 1.0},
            {'key': 'hours', 'label': '年有效小时', 'unit': 'h', 'required': True},
            {'key': 'safety_min_q_m3_s', 'label': '安全最低风量', 'unit': 'm³/s', 'required': False},
            {'key': 'accounting_year', 'label': '核算年度', 'unit': '年', 'required': False},
        ],
    },
    'motor_efficiency': {
        'version': 'v8',
        'label': '专业电机效率模型（需要轴功率）',
        'label_zh': '专业电机效率模型（需要轴功率）',
        'required_inputs': ['p_shaft_kw', 'eta_old', 'eta_new', 'hours'],
        'status': 'supported',
        'is_professional': True,
        'ui_hint': '有流量、扬程或效率记录？试试更贴近工况的测算。',
        'fields': [
            {'key': 'p_shaft_kw', 'label': '轴输出功率', 'unit': 'kW', 'required': True},
            {'key': 'eta_old', 'label': '改造前效率', 'unit': '0–1', 'required': True},
            {'key': 'eta_new', 'label': '改造后效率', 'unit': '0–1', 'required': True},
            {'key': 'hours', 'label': '年有效小时', 'unit': 'h', 'required': True},
            {'key': 'accounting_year', 'label': '核算年度', 'unit': '年', 'required': False},
        ],
    },
    'compressor_power_drop': {
        'version': 'v8',
        'label': '空压机实测功率差情景',
        'label_zh': '空压机实测功率差情景',
        'required_inputs': ['p_before_kw', 'p_after_kw', 'hours'],
        'status': 'reference_scenario',
        'is_professional': True,
        'ui_hint': '同供气任务下的实测功率差参考情景。',
        'fields': [
            {'key': 'p_before_kw', 'label': '改造前功率', 'unit': 'kW', 'required': True},
            {'key': 'p_after_kw', 'label': '改造后功率', 'unit': 'kW', 'required': True},
            {'key': 'hours', 'label': '年有效小时', 'unit': 'h', 'required': True},
            {'key': 'accounting_year', 'label': '核算年度', 'unit': '年', 'required': False},
        ],
    },
    'transformer_losses': {
        'version': 'v8',
        'label': '变压器损耗简化模型',
        'label_zh': '变压器损耗简化模型',
        'required_inputs': ['p0_before_kw', 'p0_after_kw', 'pk_before_kw', 'pk_after_kw', 'beta', 'hours'],
        'status': 'supported',
        'is_professional': True,
        'ui_hint': '有空载/负载损耗数据时可估算。',
        'fields': [
            {'key': 'p0_before_kw', 'label': '改造前空载损耗 P0', 'unit': 'kW', 'required': True},
            {'key': 'p0_after_kw', 'label': '改造后空载损耗 P0', 'unit': 'kW', 'required': True},
            {'key': 'pk_before_kw', 'label': '改造前负载损耗 Pk', 'unit': 'kW', 'required': True},
            {'key': 'pk_after_kw', 'label': '改造后负载损耗 Pk', 'unit': 'kW', 'required': True},
            {'key': 'beta', 'label': '负载系数 β', 'unit': '0–1', 'required': True},
            {'key': 'hours', 'label': '年有效小时', 'unit': 'h', 'required': True},
            {'key': 'accounting_year', 'label': '核算年度', 'unit': '年', 'required': False},
        ],
    },
}


def list_types() -> list[dict[str, Any]]:
    return [dict(t) for t in EQUIPMENT_TYPES]


def get_type(type_code: str) -> dict[str, Any] | None:
    for t in EQUIPMENT_TYPES:
        if t['type_code'] == type_code:
            return dict(t)
    return None


def resolve_type_code(device_type: str | None, name: str | None = None) -> str:
    """最长明确别名优先；禁止单字「泵」抢先；未知→generic。

    匹配顺序：
    1. 精确匹配 type_code
    2. 精确匹配标准 display_name
    3. 别名按长度降序子串匹配（同一长度按注册顺序）
    """
    raw = (device_type or '').strip()
    blob = f'{device_type or ""}{name or ""}'
    if not blob.strip():
        return 'generic'

    # 1) 稳定代码
    for t in EQUIPMENT_TYPES:
        if raw == t['type_code']:
            return t['type_code']
    # 2) 完全一致标准名
    for t in EQUIPMENT_TYPES:
        if raw == t['display_name']:
            return t['type_code']

    # 3) 最长别名优先；忽略长度=1 的泛化别名
    candidates: list[tuple[int, int, str, str]] = []
    for idx, t in enumerate(EQUIPMENT_TYPES):
        for a in t['aliases']:
            if not a or len(a) <= 1:
                continue
            if a in blob:
                candidates.append((len(a), -idx, a, t['type_code']))
    if candidates:
        candidates.sort(reverse=True)  # 长度降序，同长度注册靠前优先（-idx 大）
        return candidates[0][3]
    return 'generic'


def methods_for(type_code: str) -> list[dict[str, Any]]:
    t = get_type(type_code) or get_type('generic')
    assert t is not None
    out = []
    for m in t['supported_methods']:
        meta = dict(METHOD_META.get(m) or {'label': m, 'status': 'unknown'})
        meta['method_code'] = m
        out.append(meta)
    return out


def available_methods_zh(type_code: str) -> str:
    """设备详情中文能力说明，不露内部代码。"""
    methods = methods_for(type_code)
    parts = []
    for m in methods:
        if m.get('is_professional'):
            parts.append(m.get('label_zh') or m.get('label') or '专业模型')
        else:
            parts.append('通用比例情景' if m['method_code'] == 'baseline_fraction' else (m.get('label_zh') or '通用功率差情景'))
    # 去重保序
    seen = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return '／'.join(seen) if seen else '仅可录入'


def capability_matrix() -> list[dict[str, Any]]:
    rows = []
    for t in EQUIPMENT_TYPES:
        rows.append({
            'type_code': t['type_code'],
            'display_name': t['display_name'],
            '可录入': bool(t.get('can_register')),
            '可独立试算': bool(t.get('can_trial_calc')),
            '已接入完整决策流程': bool(t.get('in_decision_flow')),
            'supported_methods': list(t.get('supported_methods') or []),
            '目前可用': available_methods_zh(t['type_code']),
            'model_status': t.get('model_status'),
            'safety_notes': t.get('safety_notes') or '',
        })
    return rows


def method_allowed_for_type(type_code: str, method_code: str) -> bool:
    t = get_type(type_code) or get_type('generic')
    assert t is not None
    return method_code in (t.get('supported_methods') or [])


def resolve_type_detail(device_type: str | None, name: str | None = None) -> dict[str, Any]:
    """旧数据「泵」不静默改成 generic；要求确认排水泵/循环水泵，同时给出可用方法以免方法栏空白。"""
    code = resolve_type_code(device_type, name)
    blob = f'{device_type or ""}{name or ""}'
    ambiguous = code == 'generic' and '泵' in blob
    if ambiguous:
        methods = []
        seen: set[str] = set()
        for tc in ('pump_drainage', 'pump_process'):
            for m in methods_for(tc):
                if m['method_code'] not in seen:
                    seen.add(m['method_code'])
                    methods.append(m)
        return {
            'type_code': None,
            'resolved_type_code': None,
            'needs_confirm': True,
            'confirm_options': [
                {'type_code': 'pump_drainage', 'display_name': '矿井排水泵'},
                {'type_code': 'pump_process', 'display_name': '循环水泵/矿浆泵'},
            ],
            'methods': methods,
            'message': '请确认这台泵属于矿井排水泵还是循环水泵/矿浆泵。确认前仍可选择水泵类测算方法，不会静默改成通用设备。',
        }
    return {
        'type_code': code,
        'resolved_type_code': code,
        'needs_confirm': False,
        'confirm_options': [],
        'methods': methods_for(code),
        'message': '',
    }


def display_options() -> list[str]:
    """前端下拉：展示名。"""
    seen = []
    for t in EQUIPMENT_TYPES:
        short = {
            'ball_mill': '球磨机',
            'sag_mill': '半自磨机（SAG）',
            'pump_drainage': '矿井排水泵',
            'pump_process': '循环水泵/矿浆泵',
            'fan_ventilation': '矿井通风机',
            'compressor': '空气压缩机',
            'crusher': '破碎机',
            'conveyor': '带式输送机',
            'motor_drive': '高效电动机/驱动',
            'transformer': '配电变压器',
            'generic': '其他设备（仅录入/通用）',
        }.get(t['type_code'], t['display_name'])
        if short not in seen:
            seen.append(short)
    return seen


def method_compatible_with_device(device, method_code: str | None) -> bool:
    """设备类型 × 方法白名单；旧「泵」确认前允许水泵方法并集。"""
    if not method_code or device is None:
        return True
    type_code = resolve_type_code(getattr(device, 'device_type', None), getattr(device, 'name', None))
    if method_allowed_for_type(type_code, method_code):
        return True
    detail = resolve_type_detail(getattr(device, 'device_type', None), getattr(device, 'name', None))
    allowed = {m.get('method_code') for m in (detail.get('methods') or [])}
    return method_code in allowed
