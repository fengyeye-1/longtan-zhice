"""参考资料选择：互斥动作、逐层校验，并把确认值写入对应措施 model_inputs。"""
from __future__ import annotations

import uuid
from copy import deepcopy
from typing import Any

from fastapi import HTTPException

from ..calc import physics_models as phys
from . import equipment_catalog as equip_cat
from . import store
from .compute import _hours_conflict_message, recalculate_case
from ..models.schema import ReferenceSelection

CHOICE_LABELS = {
    'reject': '资料不匹配，不使用',
    'use_own': '改用自己的实测或报价',
    'match_trial': '规格基本一致，仅作参考试算',
}

NOTE_BOOKMARK = '已保存资料来源。这不会改变本次计算结果。已保存这份资料的来源；尚未把其中的数值用于计算。'
NOTE_REJECT = '这份资料与你选择的设备或改造方式不一致，不能用于当前测算。'
NOTE_USE_OWN = '下一步，请填写企业实测值。确认保存后，原先采用的参考参数会失效。点击此按钮本身不会改变当前计算结果。'
NOTE_REVOKE_NO_TARGET = '请先选择要撤销的设备、改造方案和具体参数。'
NOTE_KEEP_MANUAL = '这份资料已不再用于当前测算；你后来填写的数据会保留。'
NOTE_APPLIED_PREFIX = '已将参考参数用于'
NOTE_SAVED_ONLY = '已保存资料来源。这不会改变本次计算结果。'
NOTE_CHOOSE_MEASURE = '请先选择要测算的改造方案'
NOTE_ADD_MEASURE = '请先添加改造方案。当前设备还没有技改措施，只能收藏这份资料，不能把它当作已应用的计算参数。'

EFFICIENCY_KEYS = {
    'eta_pump_before', 'eta_pump_after', 'eta_motor', 'eta_drive',
    'eta_fan_before', 'eta_fan_after', 'eta_old', 'eta_new',
    'saving_fraction',
}

NONNEG_KEYS = {
    'q_m3_h', 'q_m3_s', 'head_m', 'delta_p_pa', 'hours',
    'p_shaft_kw', 'p_before_kw', 'p_after_kw',
    'p0_before_kw', 'p0_after_kw', 'pk_before_kw', 'pk_after_kw',
    'investment_cny', 'annual_maintenance_cny', 'energy_price',
    'power_kw', 'power_drop_kw', 'safety_min_q_m3_s',
}

FRIENDLY_NEG = {
    'q_m3_h': '流量不能为负数。请检查单位或数据；本次测算仍使用上一次确认的参数。',
    'q_m3_s': '风量不能为负数。请检查单位或数据；本次测算仍使用上一次确认的参数。',
    'head_m': '扬程不能为负数。请检查单位或数据；本次测算仍使用上一次确认的参数。',
    'delta_p_pa': '风压不能为负数。请检查单位或数据；本次测算仍使用上一次确认的参数。',
    'rho_kg_m3': '密度必须大于 0。请检查单位或数据；本次测算仍使用上一次确认的参数。',
    'hours': '小时不得为负。请检查单位或数据；本次测算仍使用上一次确认的参数。',
    'p_shaft_kw': '功率不能为负数。请检查单位或数据；本次测算仍使用上一次确认的参数。',
    'investment_cny': '成本不能为负数。请检查单位或数据；本次测算仍使用上一次确认的参数。',
    'annual_maintenance_cny': '成本不能为负数。请检查单位或数据；本次测算仍使用上一次确认的参数。',
    'energy_price': '电价不能为负数。请检查单位或数据；本次测算仍使用上一次确认的参数。',
}


def _norm_unit(unit: Any) -> str | None:
    if unit is None:
        return None
    s = str(unit).strip().replace('–', '-').replace('—', '-')
    key = s.lower().replace(' ', '')
    aliases = {
        '0-1': '0-1',
        '1': '0-1',
        'ratio': '0-1',
        '无量纲': '0-1',
        '-': '0-1',
        '%': '%',
        'kg/m³': 'kg/m3',
        'kg/m3': 'kg/m3',
        'kg/m^3': 'kg/m3',
        'm³/h': 'm3/h',
        'm3/h': 'm3/h',
        'm³/s': 'm3/s',
        'm3/s': 'm3/s',
        'kw': 'kw',
        'h': 'h',
        '小时': 'h',
        'hr': 'h',
        'm': 'm',
        'pa': 'pa',
        '年': '年',
    }
    return aliases.get(key, key)


def _fields_for_method(method_code: str | None) -> dict[str, dict]:
    if not method_code:
        return {}
    meta = equip_cat.METHOD_META.get(method_code) or {}
    return {f['key']: f for f in (meta.get('fields') or [])}


def _supported_methods(device) -> list[str]:
    code = equip_cat.resolve_type_code(getattr(device, 'device_type', None) or '')
    detail = equip_cat.get_type(code) or {}
    return list(detail.get('supported_methods') or [])


def _source_allows_method(src: dict | None, method_code: str | None) -> bool:
    if not src or not method_code:
        return True
    allowed = src.get('applicable_method_codes')
    if allowed is None:
        allowed = src.get('model_scope') or []
    if not allowed:
        return True
    return method_code in allowed


def _source_allows_device(src: dict | None, device) -> bool:
    if not src or device is None:
        return True
    allowed = src.get('applicable_type_codes')
    if not allowed:
        return True
    code = equip_cat.resolve_type_code(getattr(device, 'device_type', None) or '')
    return code in allowed


def _is_standard_not_measured(src: dict | None) -> bool:
    if not src:
        return False
    sid = str(src.get('source_id') or '')
    blob = f"{src.get('title') or ''} {src.get('note') or ''}"
    return sid.startswith('gb-') or '能效限定值' in blob or '不得凭能效等级' in blob or '国家标准' in blob


def _infer_method(device, parameter_key: str | None, method_code: str | None, src: dict | None) -> str | None:
    if method_code:
        return method_code
    if not parameter_key:
        return None
    candidates = []
    for m in _supported_methods(device):
        if parameter_key in _fields_for_method(m):
            candidates.append(m)
    if src:
        preferred = [m for m in candidates if _source_allows_method(src, m)]
        if preferred:
            return preferred[0]
    return candidates[0] if candidates else None


def _parse_number(value: Any, name: str) -> float:
    if value is None or value == '':
        raise HTTPException(400, f'{name} 不能为空')
    if isinstance(value, bool):
        raise HTTPException(400, f'{name} 必须是数值')
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ('nan', '+nan', '-nan', 'inf', '+inf', '-inf', 'infinity', '+infinity', '-infinity'):
            raise HTTPException(400, f'{name} 不能为 NaN/Infinity')
        try:
            f = float(s)
        except ValueError as e:
            raise HTTPException(400, f'{name} 无法解析为数值') from e
    else:
        try:
            f = float(value)
        except (TypeError, ValueError) as e:
            raise HTTPException(400, f'{name} 无法解析为数值') from e
    if f != f or f in (float('inf'), float('-inf')):
        raise HTTPException(400, f'{name} 不能为 NaN/Infinity')
    return f


def _is_efficiency_key(parameter_key: str | None) -> bool:
    return bool(parameter_key) and (parameter_key in EFFICIENCY_KEYS or parameter_key.startswith('eta_'))


def _validate_value(parameter_key: str, value: float) -> None:
    if _is_efficiency_key(parameter_key):
        if not (0 < value <= 1):
            raise HTTPException(400, f'{parameter_key} 须在 (0,1]，不能使用 {value}')
    if parameter_key == 'beta' and not (0 <= value <= 1):
        raise HTTPException(400, '负载系数须在 0—1')
    if parameter_key == 'rho_kg_m3' and value <= 0:
        raise HTTPException(400, FRIENDLY_NEG['rho_kg_m3'])
    if parameter_key in NONNEG_KEYS and value < 0:
        raise HTTPException(400, FRIENDLY_NEG.get(
            parameter_key,
            f'{parameter_key} 不能为负数。请检查单位或数据；本次测算仍使用上一次确认的参数。',
        ))
    limits = getattr(phys, 'FIELD_LIMITS', None) or {}
    if parameter_key in limits:
        lo, hi = limits[parameter_key]
        if value < lo or value > hi:
            if value < 0 and parameter_key in FRIENDLY_NEG:
                raise HTTPException(400, FRIENDLY_NEG[parameter_key])
            raise HTTPException(400, f'{parameter_key} 超出合理范围。请检查单位或数据；本次测算仍使用上一次确认的参数。')


def _validate_unit(parameter_key: str, unit: Any, method_code: str | None) -> str:
    if not unit:
        raise HTTPException(400, '缺少单位，无法应用参考参数')
    got = _norm_unit(unit)
    fields = _fields_for_method(method_code)
    expected_raw = (fields.get(parameter_key) or {}).get('unit')
    expected = _norm_unit(expected_raw) if expected_raw else None
    if expected and got != expected:
        raise HTTPException(400, f'单位与参数不匹配：{parameter_key} 应为 {expected_raw}，不能写成 {unit}')
    if _is_efficiency_key(parameter_key) and got not in ('0-1', '%'):
        raise HTTPException(400, f'效率参数单位应为 0-1，不能写成 {unit}')
    return str(unit)


def _find_device(case, device_id: str | None):
    if not device_id:
        return None
    return next((d for d in (case.devices or []) if d.id == device_id), None)


def _measure_method(m) -> str | None:
    return getattr(m, 'method_code', None) or getattr(m, 'calculation_method', None)


def _find_target_measures(case, *, device_id: str | None, method_code: str | None, measure_id: str | None, unique: bool = True):
    if measure_id:
        m = next((x for x in (case.retrofits or []) if x.id == measure_id), None)
        if m is None:
            raise HTTPException(400, '目标措施不存在')
        if device_id and m.device_id != device_id:
            raise HTTPException(400, '目标措施不属于该设备，不能跨设备应用')
        if method_code and _measure_method(m) != method_code:
            raise HTTPException(400, NOTE_REJECT)
        return [m]
    out = []
    for m in (case.retrofits or []):
        if device_id and m.device_id != device_id:
            continue
        if method_code and _measure_method(m) != method_code:
            continue
        out.append(m)
    if unique and len(out) > 1:
        raise HTTPException(400, NOTE_CHOOSE_MEASURE)
    return out


def _already_applied(case, *, device_id, parameter_key, source_id, value, measure_id=None) -> bool:
    for x in (getattr(case, 'parameter_values', None) or []):
        if (
            x.get('device_id') == device_id
            and x.get('key') == parameter_key
            and x.get('source_id') == source_id
            and x.get('applied')
            and (measure_id is None or (x.get('measure_id') or None) in (None, measure_id))
        ):
            try:
                if abs(float(x.get('value')) - float(value)) < 1e-12:
                    return True
            except (TypeError, ValueError):
                if x.get('value') == value:
                    return True
    return False


def _slot_of(sel) -> tuple:
    return (
        getattr(sel, 'applicable_device_id', None) or None,
        getattr(sel, 'measure_id', None) or None,
        getattr(sel, 'method_code', None) or None,
        getattr(sel, 'parameter_key', None) or None,
    )


def _same_identity(existing, *, source_id, device_id, method_code, measure_id, parameter_key) -> bool:
    return (
        existing.source_id == source_id
        and (existing.applicable_device_id or None) == (device_id or None)
        and (existing.method_code or None) == (method_code or None)
        and (getattr(existing, 'measure_id', None) or None) == (measure_id or None)
        and (existing.parameter_key or None) == (parameter_key or None)
    )


def _sel_version(sel) -> tuple:
    return (getattr(sel, 'version', None) or 0, getattr(sel, 'selected_at', None) or '')


def _was_apply(sel) -> bool:
    return getattr(sel, 'apply_intent', None) == 'apply_to_trial' and sel.choice == 'match_trial'


def _previous_valid_binding(case, current):
    slot = _slot_of(current)
    older_valid = []
    applies = []
    for sel in (case.reference_selections or []):
        if sel is current:
            continue
        if _slot_of(sel) != slot:
            continue
        if _was_apply(sel):
            applies.append(sel)
        if getattr(sel, 'revoked', False) or sel.choice == 'reject':
            continue
        if not _was_apply(sel):
            continue
        if _sel_version(sel) < _sel_version(current):
            older_valid.append(sel)
    if older_valid:
        best = max(older_valid, key=_sel_version)
        return getattr(best, 'parameter_value', None), best
    if applies:
        first = min(applies, key=_sel_version)
        return getattr(first, 'previous_value', None), None
    return getattr(current, 'previous_value', None), None


def _friendly_phys_error(parameter_key: str | None, raw: str | None) -> str:
    msg = str(raw or '')
    if parameter_key and parameter_key in FRIENDLY_NEG and ('不得为负' in msg or '负数' in msg or '不能为负' in msg):
        return FRIENDLY_NEG[parameter_key]
    if '不得为负' in msg and parameter_key == 'q_m3_h':
        return FRIENDLY_NEG['q_m3_h']
    if parameter_key == 'q_m3_h' and ('负' in msg or '非法' in msg or '范围' in msg):
        return FRIENDLY_NEG['q_m3_h']
    if msg:
        return f'{msg}。请检查单位或数据；本次测算仍使用上一次确认的参数。'
    return '参数不合法。请检查单位或数据；本次测算仍使用上一次确认的参数。'


def _validate_written_models(trial, targets, parameter_key: str | None) -> None:
    for m in targets:
        method = _measure_method(m)
        if not method or method not in phys.PROFESSIONAL_METHOD_CODES:
            continue
        raw = phys.run_method(method, dict(getattr(m, 'model_inputs', None) or {}))
        if raw.get('status') == 'invalid_input':
            raise HTTPException(400, _friendly_phys_error(parameter_key, raw.get('error')))


def _hours_would_conflict(device, parameter_key, parsed_value, measure) -> bool:
    if device is None:
        return False
    device_hours = getattr(device, 'hours', None)
    if device_hours is None:
        return False
    hours = parsed_value if parameter_key == 'hours' else (getattr(measure, 'model_inputs', None) or {}).get('hours')
    if hours is None or hours == '':
        return False
    try:
        return float(hours) > float(device_hours)
    except (TypeError, ValueError):
        return False


def _extractable_items(src: dict | None) -> list[dict]:
    if not src:
        return []
    items = src.get('extractable_parameters') or src.get('documented_parameters') or []
    return [x for x in items if isinstance(x, dict)]


def _documented_item(src: dict | None, parameter_key: str | None, parsed_value) -> dict | None:
    if not src or not parameter_key:
        return None
    for item in _extractable_items(src):
        key = item.get('key') or item.get('parameter_key') or item.get('parameter_type')
        if key != parameter_key:
            continue
        if item.get('value') is None:
            return item
        try:
            if abs(float(item.get('value')) - float(parsed_value)) < 1e-6:
                return item
        except (TypeError, ValueError):
            continue
    return None


def _classify_value_kind(src: dict | None, parameter_key: str | None, parsed_value):
    if not src:
        return '用户假设', None
    documented = _documented_item(src, parameter_key, parsed_value)
    if documented:
        locator = documented.get('excerpt_locator') or documented.get('locator') or documented.get('excerpt')
        return '资料摘录', locator
    return '用户假设', None


def _assumption_note(src: dict | None, parameter_key: str | None, parsed_value, value_kind: str | None) -> str | None:
    if value_kind == '资料摘录':
        return None
    if not _is_efficiency_key(parameter_key):
        return None
    try:
        pretty = f'{float(parsed_value):.2f}'
    except (TypeError, ValueError):
        pretty = str(parsed_value)
    return (
        f'这份资料能解释计算方法，但没有提供这台设备的效率。'
        f'你填写的{pretty}将作为试算假设，而不是资料里的实测值。'
    )


def is_current_writer(case, sel) -> bool:
    """绑定仍是该参数当前有效写入者：未撤销/未覆盖，且值与 model_inputs 一致。"""
    if getattr(sel, 'revoked', False) or getattr(sel, 'superseded', False):
        return False
    if not (getattr(sel, 'applied', False) or getattr(sel, 'active', False)):
        return False
    key = getattr(sel, 'parameter_key', None)
    if not key:
        return False
    measure_id = getattr(sel, 'measure_id', None)
    m = next((x for x in (case.retrofits or []) if x.id == measure_id), None)
    if m is None:
        return False
    if key == 'power_drop_kw':
        current = getattr(m, 'power_drop_kw', None)
    elif key == 'saving_fraction':
        current = getattr(m, 'saving_fraction', None)
    else:
        current = (getattr(m, 'model_inputs', None) or {}).get(key)
    if current is None or getattr(sel, 'parameter_value', None) is None:
        return False
    try:
        return abs(float(current) - float(sel.parameter_value)) < 1e-9
    except (TypeError, ValueError):
        return current == sel.parameter_value


def _has_revoke_target(*, source_id, device_id, parameter_key, method_code, measure_id, binding_id) -> bool:
    if binding_id:
        return True
    if source_id and measure_id:
        return True
    # 单设备 + 参数键也可唯一定位（旧客户端未传 measure_id）
    if source_id and device_id and parameter_key:
        return True
    return False


def _restore_applied(case, *, source_id, device_id, parameter_key, method_code, measure_id=None, binding_id=None) -> bool:
    if not _has_revoke_target(
        source_id=source_id, device_id=device_id, parameter_key=parameter_key,
        method_code=method_code, measure_id=measure_id, binding_id=binding_id,
    ):
        raise HTTPException(400, NOTE_REVOKE_NO_TARGET)
    restored = False
    matched = []
    for sel in list(case.reference_selections or []):
        if binding_id and getattr(sel, 'binding_id', None) != binding_id:
            continue
        if source_id and sel.source_id != source_id:
            continue
        if device_id and (sel.applicable_device_id or None) != device_id:
            continue
        if measure_id and (getattr(sel, 'measure_id', None) or None) not in (None, measure_id):
            continue
        if parameter_key and sel.parameter_key not in (None, parameter_key):
            continue
        if method_code and (sel.method_code or None) not in (None, method_code):
            continue
        matched.append(sel)
    applied_mids = {
        getattr(sel, 'measure_id', None)
        for sel in matched
        if (getattr(sel, 'applied', False) or getattr(sel, 'active', False)) and not getattr(sel, 'revoked', False)
    }
    applied_mids.discard(None)
    if not binding_id and not measure_id and len(applied_mids) > 1:
        raise HTTPException(400, NOTE_REVOKE_NO_TARGET)
    if binding_id:
        if not matched:
            raise HTTPException(400, NOTE_REVOKE_NO_TARGET)
        # 核对所属身份
        sel0 = matched[0]
        if measure_id and (getattr(sel0, 'measure_id', None) or None) not in (None, measure_id):
            raise HTTPException(400, NOTE_REVOKE_NO_TARGET)
        if device_id and (sel0.applicable_device_id or None) not in (None, device_id):
            raise HTTPException(400, NOTE_REVOKE_NO_TARGET)
        if parameter_key and sel0.parameter_key not in (None, parameter_key):
            raise HTTPException(400, NOTE_REVOKE_NO_TARGET)

    for sel in matched:
        if getattr(sel, 'revoked', False) and not getattr(sel, 'applied', False) and not getattr(sel, 'active', False):
            continue
        was_writer = is_current_writer(case, sel)
        is_active = bool(getattr(sel, 'applied', False) or getattr(sel, 'active', False))
        sel.revoked = True
        sel.applied = False
        sel.active = False
        if not was_writer:
            continue
        if not is_active:
            continue
        key = parameter_key or sel.parameter_key
        if not key:
            continue
        restore_val, restore_sel = _previous_valid_binding(case, sel)
        target_measure = getattr(sel, 'measure_id', None) or measure_id
        try:
            targets = _find_target_measures(
                case,
                device_id=sel.applicable_device_id or device_id,
                method_code=sel.method_code or method_code,
                measure_id=target_measure,
                unique=False,
            )
        except HTTPException:
            targets = []
        if not target_measure and len(targets) > 1:
            targets = []
        write_val = restore_val if restore_val is not None else (
            getattr(restore_sel, 'parameter_value', None) if restore_sel is not None else getattr(sel, 'previous_value', None)
        )
        for m in targets:
            inputs = dict(getattr(m, 'model_inputs', None) or {})
            if write_val is None:
                if key in inputs:
                    inputs.pop(key, None)
                    m.model_inputs = inputs
                    restored = True
            else:
                inputs[key] = write_val
                m.model_inputs = inputs
                restored = True
        if restore_sel is not None:
            restore_sel.applied = True
            restore_sel.active = True
            restore_sel.revoked = False
            restore_sel.superseded = False
        pvs = []
        for x in (getattr(case, 'parameter_values', None) or []):
            same_slot = (
                x.get('device_id') == (sel.applicable_device_id or device_id)
                and x.get('key') == key
                and (x.get('measure_id') or None) in (None, target_measure, getattr(sel, 'measure_id', None))
            )
            if same_slot and x.get('source_id') == sel.source_id:
                restored = True
                continue
            pvs.append(x)
        if restore_sel is not None:
            pvs = [
                x for x in pvs
                if not (
                    x.get('device_id') == (restore_sel.applicable_device_id or device_id)
                    and x.get('key') == key
                    and (x.get('measure_id') or None) in (None, restore_sel.measure_id)
                )
            ]
            pvs.append({
                'device_id': restore_sel.applicable_device_id or device_id,
                'key': key,
                'value': restore_sel.parameter_value,
                'unit': restore_sel.unit,
                'source_id': restore_sel.source_id,
                'applied': True,
                'method_code': restore_sel.method_code,
                'value_kind': restore_sel.value_kind,
                'measure_id': restore_sel.measure_id,
                'binding_id': restore_sel.binding_id,
            })
        case.parameter_values = pvs
    return restored


def apply_reference_selection(body: dict, *, src: dict | None, registry_version: str, selected_at: str) -> dict:
    choice = (body or {}).get('choice')
    if choice not in ('match_trial', 'reject', 'use_own'):
        raise HTTPException(400, 'choice 须为 match_trial / reject / use_own')

    source_id = (body or {}).get('source_id')
    device_id = (body or {}).get('applicable_device_id') or (body or {}).get('device_id')
    measure_id = (body or {}).get('measure_id') or (body or {}).get('applicable_measure_id')
    method_code = (body or {}).get('method_code')
    apply_intent = (body or {}).get('apply_intent') or (body or {}).get('intent') or 'bookmark'
    if (body or {}).get('confirm_for_calc') or (body or {}).get('apply_to_trial'):
        if apply_intent != 'preview':
            apply_intent = 'apply_to_trial'
    parameter_key = (body or {}).get('parameter_key') or (body or {}).get('field_key')
    parameter_value = (body or {}).get('parameter_value')
    if parameter_value is None:
        parameter_value = (body or {}).get('value_if_any')
    unit = (body or {}).get('unit')
    value_if_any = (body or {}).get('value_if_any')
    binding_id = (body or {}).get('binding_id')

    wants_apply = (
        choice == 'match_trial'
        and apply_intent == 'apply_to_trial'
        and parameter_value is not None
    )
    wants_preview = choice == 'match_trial' and apply_intent == 'preview'

    case = store.get_case()
    device = _find_device(case, device_id) if device_id else None

    if choice in ('reject', 'use_own'):
        trial = deepcopy(case)
        restored = False
        if choice == 'reject':
            can_restore = _has_revoke_target(
                source_id=source_id, device_id=device_id, parameter_key=parameter_key,
                method_code=method_code, measure_id=measure_id, binding_id=binding_id,
            )
            if not can_restore:
                live = [
                    s for s in (trial.reference_selections or [])
                    if (getattr(s, 'applied', False) or getattr(s, 'active', False)) and not getattr(s, 'revoked', False)
                ]
                if live:
                    raise HTTPException(400, NOTE_REVOKE_NO_TARGET)
                restored = False
            else:
                restored = _restore_applied(
                    trial, source_id=source_id, device_id=device_id,
                    parameter_key=parameter_key, method_code=method_code,
                    measure_id=measure_id, binding_id=binding_id,
                )
        if choice == 'use_own' and parameter_value is not None and parameter_key and measure_id:
            parsed_own = _parse_number(parameter_value, parameter_key)
            _validate_value(parameter_key, parsed_own)
            _validate_unit(parameter_key, unit, method_code)
            targets = _find_target_measures(
                trial, device_id=device_id, method_code=method_code, measure_id=measure_id, unique=True,
            )
            for m in targets:
                inputs = dict(getattr(m, 'model_inputs', None) or {})
                inputs[parameter_key] = parsed_own
                m.model_inputs = inputs
            from .change_validation import supersede_bindings_for_manual_edit
            tgt = next((x for x in trial.retrofits if x.id == measure_id), None)
            if tgt is not None:
                supersede_bindings_for_manual_edit(trial, tgt, [parameter_key], reason='用户改用自己的数据')
        if source_id:
            sel = ReferenceSelection(
                source_id=source_id,
                verified_by_user=True,
                applicable_device_id=device_id,
                method_code=method_code,
                value_if_any=value_if_any,
                source_version=(src or {}).get('issue_date') or (src or {}).get('checked_at') or registry_version,
                selected_at=selected_at,
                choice=choice,
                title=(src or {}).get('title'),
                source_url=(src or {}).get('source_url'),
                applied=False,
                parameter_key=parameter_key,
                parameter_value=parameter_value,
                unit=unit,
                apply_intent='bookmark',
                measure_id=measure_id,
                value_kind=None,
                binding_id=str(uuid.uuid4()),
                active=False,
                revoked=choice == 'reject',
                source_year=(src or {}).get('issue_date'),
            )
            kept = [
                existing for existing in (trial.reference_selections or [])
                if not (
                    _same_identity(
                        existing, source_id=source_id, device_id=device_id,
                        method_code=method_code, measure_id=measure_id, parameter_key=parameter_key,
                    )
                    and not _was_apply(existing)
                )
            ]
            kept.append(sel)
            trial.reference_selections = kept
        saved = store.commit_case(trial)
        results = recalculate_case(saved)
        note = NOTE_REJECT if choice == 'reject' else NOTE_USE_OWN
        if restored:
            note = '已撤销该参考参数，并恢复先前有效值。能耗、减碳、经济数据和报告已同步。'
        elif choice == 'reject':
            note = NOTE_KEEP_MANUAL if (binding_id or measure_id) else NOTE_REJECT
        return _pack(
            saved, src, choice, False, note, selected_at, registry_version,
            source_id, device_id, method_code, value_if_any, parameter_key, parameter_value, unit,
            results=results, restored=restored, measure_id=measure_id,
        )

    parsed_value = None
    value_kind = None
    excerpt_locator = None
    targets = []
    if wants_apply or wants_preview:
        if device_id and device is None:
            raise HTTPException(400, '设备不存在')
        if not device_id:
            raise HTTPException(400, '缺少设备，无法把参考参数用于测算')
        method_code = _infer_method(device, parameter_key, method_code, src)
        if method_code and not _source_allows_method(src, method_code):
            raise HTTPException(400, NOTE_REJECT)
        if not _source_allows_device(src, device):
            raise HTTPException(400, NOTE_REJECT)
        if not parameter_key:
            raise HTTPException(400, '缺少 parameter_key，无法应用参考参数')
        fields = _fields_for_method(method_code)
        if method_code and fields and parameter_key not in fields:
            raise HTTPException(400, f'参数 {parameter_key} 不属于方法 {method_code}')
        if method_code and device and method_code not in _supported_methods(device) and parameter_key not in fields:
            raise HTTPException(400, NOTE_REJECT)
        parsed_value = _parse_number(parameter_value, parameter_key)
        _validate_value(parameter_key, parsed_value)
        _validate_unit(parameter_key, unit, method_code)
        if measure_id or method_code:
            targets = _find_target_measures(
                case, device_id=device_id, method_code=method_code, measure_id=measure_id, unique=True,
            )
        if measure_id and not targets:
            raise HTTPException(400, '目标措施不属于该设备，不能跨设备应用')
        if not measure_id and len(targets) == 1:
            measure_id = targets[0].id
        value_kind, excerpt_locator = _classify_value_kind(src, parameter_key, parsed_value)

    assumption_note = _assumption_note(src, parameter_key, parsed_value, value_kind)

    if wants_preview:
        if not targets:
            return _pack(
                case, src, choice, False, NOTE_ADD_MEASURE, selected_at, registry_version,
                source_id, device_id, method_code, value_if_any, parameter_key, parsed_value, unit,
                results=recalculate_case(case), value_kind=value_kind, measure_id=measure_id,
                preview={
                    'old_value': None,
                    'new_value': parsed_value,
                    'old_saved_kwh': None,
                    'new_saved_kwh': None,
                    'source_title': (src or {}).get('title'),
                    'device_name': getattr(device, 'name', None),
                    'measure_names': [],
                    'value_kind': value_kind,
                    'assumption_note': assumption_note,
                },
            )
        trial = deepcopy(case)
        preview = _write_into_case(
            trial, targets=targets, device_id=device_id, method_code=method_code,
            parameter_key=parameter_key, parsed_value=parsed_value, unit=unit,
            source_id=source_id, src=src, selected_at=selected_at,
            registry_version=registry_version, value_if_any=value_if_any,
            value_kind=value_kind, measure_id=measure_id, choice=choice,
            excerpt_locator=excerpt_locator,
        )
        _validate_written_models(trial, targets if targets else _find_target_measures(
            trial, device_id=device_id, method_code=method_code, measure_id=measure_id, unique=True,
        ), parameter_key)
        after = recalculate_case(trial)
        before = recalculate_case(case)
        bsa = (before or {}).get('selected_assessment') or {}
        asa = (after or {}).get('selected_assessment') or {}
        note = '预览差异，尚未写入计算。'
        if assumption_note:
            note = note + assumption_note
        return _pack(
            case, src, choice, False, note, selected_at, registry_version,
            source_id, device_id, method_code, value_if_any, parameter_key, parsed_value, unit,
            results=before,
            preview={
                'old_value': preview.get('previous_value'),
                'new_value': parsed_value,
                'old_saved_kwh': bsa.get('saved_kwh'),
                'new_saved_kwh': asa.get('saved_kwh'),
                'source_title': (src or {}).get('title'),
                'device_name': getattr(device, 'name', None),
                'measure_names': [getattr(m, 'name', None) for m in targets],
                'value_kind': value_kind,
                'assumption_note': assumption_note,
                'excerpt_locator': excerpt_locator,
            },
            value_kind=value_kind, measure_id=measure_id,
        )

    if not wants_apply:
        trial = deepcopy(case)
        if source_id:
            sel = ReferenceSelection(
                source_id=source_id,
                verified_by_user=True,
                applicable_device_id=device_id,
                method_code=method_code,
                value_if_any=value_if_any,
                source_version=(src or {}).get('issue_date') or (src or {}).get('checked_at') or registry_version,
                selected_at=selected_at,
                choice=choice,
                title=(src or {}).get('title'),
                source_url=(src or {}).get('source_url'),
                applied=False,
                parameter_key=parameter_key,
                parameter_value=parameter_value,
                unit=unit,
                apply_intent='bookmark',
                measure_id=measure_id,
                value_kind=None,
                binding_id=str(uuid.uuid4()),
                active=False,
                source_year=(src or {}).get('issue_date'),
            )
            kept = [
                existing for existing in (trial.reference_selections or [])
                if not (
                    _same_identity(
                        existing, source_id=source_id, device_id=device_id,
                        method_code=method_code, measure_id=measure_id, parameter_key=parameter_key,
                    )
                    and not _was_apply(existing)
                )
            ]
            kept.append(sel)
            trial.reference_selections = kept
        saved = store.commit_case(trial)
        results = recalculate_case(saved)
        return _pack(
            saved, src, choice, False, NOTE_BOOKMARK, selected_at, registry_version,
            source_id, device_id, method_code, value_if_any, parameter_key, parameter_value, unit,
            results=results, measure_id=measure_id,
        )

    if not targets:
        if _is_efficiency_key(parameter_key):
            trial = deepcopy(case)
            if source_id:
                sel = ReferenceSelection(
                    source_id=source_id,
                    verified_by_user=True,
                    applicable_device_id=device_id,
                    method_code=method_code,
                    value_if_any=value_if_any,
                    source_version=(src or {}).get('issue_date') or (src or {}).get('checked_at') or registry_version,
                    selected_at=selected_at,
                    choice=choice,
                    title=(src or {}).get('title'),
                    source_url=(src or {}).get('source_url'),
                    applied=False,
                    parameter_key=parameter_key,
                    parameter_value=parsed_value,
                    unit=unit,
                    apply_intent='bookmark',
                    measure_id=measure_id,
                    value_kind=value_kind,
                    binding_id=str(uuid.uuid4()),
                    active=False,
                    source_year=(src or {}).get('issue_date'),
                    excerpt_locator=excerpt_locator,
                )
                kept = list(trial.reference_selections or [])
                kept.append(sel)
                trial.reference_selections = kept
            saved = store.commit_case(trial)
            results = recalculate_case(saved)
            return _pack(
                saved, src, choice, False, NOTE_ADD_MEASURE, selected_at, registry_version,
                source_id, device_id, method_code, value_if_any, parameter_key, parsed_value, unit,
                results=results, value_kind=value_kind, measure_id=measure_id,
            )

    if targets and _hours_would_conflict(device, parameter_key, parsed_value, targets[0]):
        trial = deepcopy(case)
        sel = ReferenceSelection(
            source_id=source_id or 'user-input',
            verified_by_user=True,
            applicable_device_id=device_id,
            method_code=method_code,
            value_if_any=value_if_any,
            source_version=(src or {}).get('issue_date') or (src or {}).get('checked_at') or registry_version,
            selected_at=selected_at,
            choice=choice,
            title=(src or {}).get('title'),
            source_url=(src or {}).get('source_url'),
            applied=False,
            parameter_key=parameter_key,
            parameter_value=parsed_value,
            unit=unit,
            apply_intent='pending_draft',
            measure_id=measure_id or (targets[0].id if targets else None),
            value_kind=value_kind or '用户假设',
            binding_id=str(uuid.uuid4()),
            active=False,
            source_year=(src or {}).get('issue_date'),
        )
        kept = list(trial.reference_selections or [])
        kept.append(sel)
        trial.reference_selections = kept
        saved = store.commit_case(trial)
        results = recalculate_case(saved)
        dh = getattr(device, 'hours', None)
        note = _hours_conflict_message(parsed_value if parameter_key == 'hours' else None, dh)
        note = note + ' 已保存为待修改草稿，不能作为已应用的有效参数，也不会给出可信节电或经济推荐。'
        return _pack(
            saved, src, choice, False, note, selected_at, registry_version,
            source_id, device_id, method_code, value_if_any, parameter_key, parsed_value, unit,
            results=results, value_kind=value_kind, measure_id=measure_id, draft=True,
        )

    if _already_applied(case, device_id=device_id, parameter_key=parameter_key, source_id=source_id, value=parsed_value, measure_id=measure_id):
        same_inputs = True
        for m in targets:
            inputs = dict(getattr(m, 'model_inputs', None) or {})
            try:
                if abs(float(inputs.get(parameter_key)) - float(parsed_value)) > 1e-12:
                    same_inputs = False
                    break
            except (TypeError, ValueError):
                same_inputs = False
                break
        if same_inputs:
            results = recalculate_case(case)
            note = _applied_note(device, targets, driven=bool(targets))
            if assumption_note:
                note = note + assumption_note
            return _pack(
                case, src, choice, True, note, selected_at, registry_version,
                source_id, device_id, method_code, value_if_any, parameter_key, parsed_value, unit,
                results=results, value_kind=value_kind, measure_id=measure_id,
            )

    trial = deepcopy(case)
    try:
        written_targets = _find_target_measures(
            trial, device_id=device_id, method_code=method_code, measure_id=measure_id, unique=True,
        )
        _write_into_case(
            trial, targets=written_targets,
            device_id=device_id, method_code=method_code,
            parameter_key=parameter_key, parsed_value=parsed_value, unit=unit,
            source_id=source_id, src=src, selected_at=selected_at,
            registry_version=registry_version, value_if_any=value_if_any,
            value_kind=value_kind, measure_id=measure_id, choice=choice,
            excerpt_locator=excerpt_locator,
        )
        _validate_written_models(trial, written_targets, parameter_key)
        results = recalculate_case(trial)
        sa = (results or {}).get('selected_assessment') or {}
        if sa.get('status') == 'invalid_input' and written_targets:
            raise HTTPException(400, _friendly_phys_error(parameter_key, sa.get('error') or sa.get('waiting_message')))
        saved = store.commit_case(trial)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f'应用参考参数失败，已回滚：{e}') from e

    driven = bool(targets or written_targets)
    note = _applied_note(device, written_targets or targets, driven=driven)
    if assumption_note:
        note = note + assumption_note
    return _pack(
        saved, src, choice, True, note, selected_at, registry_version,
        source_id, device_id, method_code, value_if_any, parameter_key, parsed_value, unit,
        results=results, value_kind=value_kind, measure_id=measure_id,
    )


def _applied_note(device, targets, *, driven: bool) -> str:
    if not driven:
        return NOTE_SAVED_ONLY + '当前没有可更新的对应措施，数值仅保存为资料，不参与计算。'
    names = []
    dname = getattr(device, 'name', None) or '设备'
    if targets:
        names = [f'{dname}—{getattr(m, "name", None) or getattr(m, "id", "")}' for m in targets]
    used = '、'.join(names) if names else dname
    return f'{NOTE_APPLIED_PREFIX}【{used}】，结果已重新计算。'


def _write_into_case(
    case, *, targets, device_id, method_code, parameter_key, parsed_value, unit,
    source_id, src, selected_at, registry_version, value_if_any, value_kind, measure_id, choice,
    excerpt_locator=None,
) -> dict:
    previous_value = None
    resolved_measure = measure_id or (targets[0].id if targets else None)
    for m in targets:
        inputs = dict(getattr(m, 'model_inputs', None) or {})
        if previous_value is None and parameter_key in inputs:
            previous_value = inputs.get(parameter_key)
        inputs[parameter_key] = parsed_value
        m.model_inputs = inputs

    pvs = [
        x for x in (getattr(case, 'parameter_values', None) or [])
        if not (
            x.get('device_id') == device_id
            and x.get('key') == parameter_key
            and (x.get('measure_id') or None) in (None, resolved_measure)
        )
    ]
    new_binding = str(uuid.uuid4())
    slot_existing = [
        existing for existing in (case.reference_selections or [])
        if (
            (existing.applicable_device_id or None) == (device_id or None)
            and (existing.method_code or None) == (method_code or None)
            and (getattr(existing, 'measure_id', None) or None) == (resolved_measure or None)
            and (existing.parameter_key or None) == (parameter_key or None)
        )
    ]
    version = (max((getattr(s, 'version', None) or 0) for s in slot_existing) + 1) if slot_existing else 1
    for existing in slot_existing:
        if getattr(existing, 'applied', False) or getattr(existing, 'active', False):
            existing.applied = False
            existing.active = False
            existing.superseded = True

    pvs.append({
        'device_id': device_id,
        'key': parameter_key,
        'value': parsed_value,
        'unit': unit,
        'source_id': source_id,
        'applied': True,
        'method_code': method_code,
        'value_kind': value_kind,
        'measure_id': resolved_measure,
        'binding_id': new_binding,
        'excerpt_locator': excerpt_locator,
    })
    case.parameter_values = pvs

    if source_id:
        sel = ReferenceSelection(
            source_id=source_id,
            verified_by_user=True,
            applicable_device_id=device_id,
            method_code=method_code,
            value_if_any=value_if_any,
            source_version=(src or {}).get('issue_date') or (src or {}).get('checked_at') or registry_version,
            selected_at=selected_at,
            choice=choice,
            title=(src or {}).get('title'),
            source_url=(src or {}).get('source_url'),
            applied=True,
            parameter_key=parameter_key,
            parameter_value=parsed_value,
            unit=unit,
            apply_intent='apply_to_trial',
            previous_value=previous_value,
            measure_id=resolved_measure,
            value_kind=value_kind,
            binding_id=new_binding,
            version=version,
            active=True,
            revoked=False,
            superseded=False,
            excerpt_locator=excerpt_locator,
            source_year=(src or {}).get('issue_date'),
        )
        kept = [
            existing for existing in (case.reference_selections or [])
            if not (
                _same_identity(
                    existing, source_id=source_id, device_id=device_id,
                    method_code=method_code, measure_id=resolved_measure, parameter_key=parameter_key,
                )
                and not _was_apply(existing)
            )
        ]
        kept.append(sel)
        case.reference_selections = kept
    return {'previous_value': previous_value, 'binding_id': new_binding, 'version': version}


def _pack(
    case, src, choice, applied, note, selected_at, registry_version,
    source_id, device_id, method_code, value_if_any, parameter_key, parameter_value, unit,
    *, results=None, preview=None, restored=False, value_kind=None, measure_id=None, draft=False,
) -> dict:
    out = {
        'ok': True,
        'applied': applied,
        'reference_trial': choice == 'match_trial',
        'label': CHOICE_LABELS[choice],
        'watermark': '参考试算' if choice == 'match_trial' else None,
        'source': src,
        'parameter_values': list(getattr(case, 'parameter_values', None) or []),
        'selection': {
            'source_id': source_id,
            'verified_by_user': True,
            'applicable_device_id': device_id,
            'method_code': method_code,
            'value_if_any': value_if_any,
            'source_version': (src or {}).get('issue_date') or (src or {}).get('checked_at') or registry_version,
            'selected_at': selected_at,
            'choice': choice,
            'applied': applied,
            'parameter_key': parameter_key,
            'parameter_value': parameter_value,
            'unit': unit,
            'value_kind': value_kind,
            'measure_id': measure_id,
            'source_url': (src or {}).get('source_url'),
            'source_year': (src or {}).get('issue_date'),
            'title': (src or {}).get('title'),
        },
        'reference_selections': [s.model_dump() for s in (case.reference_selections or [])],
        'note': note,
        'restored': restored,
        'draft': draft,
    }
    if results is not None:
        out['results'] = results
        out['selected_assessment'] = (results or {}).get('selected_assessment')
    if preview is not None:
        out['preview'] = preview
    return out
