"""统一校验入口：新增/编辑/复制/改绑/恢复技改前必须通过同一套规则。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from ..calc import physics_models as phys
from ..models.schema import PROFESSIONAL_METHOD_CODES, RetrofitProject
from . import equipment_catalog as equip_cat

MSG_NEG_INVEST = '投资金额不能小于0元，本次没有保存。请填写实际预算或留空。'
MSG_FRACTION = '预计节电幅度不能超过100%。请填写0—100之间的百分数。'
MSG_POWER = '预计降低的功率超过设备原来的功率，请核对设备规格和运行条件。'
MSG_NEG_MAINT = '年新增维护费不能小于0元，本次没有保存。若企业确有维护费用下降，请单独核算年维护费用节约。'
MSG_NAN = '输入不是有效数值，本次没有保存。'
MSG_UNCOMPUTABLE = '未保存，原结果保留，原因：修改后无法按当前方法完成测算。请核对参数，或选择只保存草稿。'
MSG_METHOD_DEVICE = '设备类型已更改。原计算方法不再适用，请重新选择测算方法。'
MSG_STALE = '数据已被其他页面修改，请刷新后再试。'
MSG_COPIED = '从原措施复制·待核实'


class ChangeRejected(Exception):
    def __init__(self, message: str, *, field: str | None = None, errors: list | None = None, status: int = 400):
        super().__init__(message)
        self.message = message
        self.field = field
        self.errors = errors or ([message] if message else [])
        self.status = status


def _friendly_validation(err: ValidationError | ValueError | Exception) -> str:
    text = str(err)
    low = text.lower()
    if 'investment' in text or '投资' in text:
        if '负' in text or 'nan' in low or 'inf' in low:
            return MSG_NEG_INVEST if '负' in text else MSG_NAN
    if 'saving_fraction' in text or '节电比例' in text or '节电幅度' in text:
        return MSG_FRACTION
    if 'annual_maintenance' in text or '维护' in text:
        return MSG_NEG_MAINT
    if 'nan' in low or 'infinity' in low or '无法解析' in text:
        return MSG_NAN
    if 'power_drop' in text and '负' in text:
        return MSG_POWER
    # pydantic wraps field messages
    for e in getattr(err, 'errors', lambda: [])():
        loc = '.'.join(str(x) for x in (e.get('loc') or ()))
        msg = e.get('msg') or ''
        blob = f'{loc} {msg}'
        if 'investment' in blob:
            return MSG_NEG_INVEST if ('负' in msg or 'greater' in msg.lower()) else MSG_NAN
        if 'saving_fraction' in blob or '节电' in msg:
            return MSG_FRACTION
        if 'annual_maintenance' in blob or '维护' in msg:
            return MSG_NEG_MAINT
        if 'nan' in msg.lower() or 'infinity' in msg.lower() or '无法解析' in msg:
            return MSG_NAN
        if msg:
            return msg
    return text or '未保存，原结果保留，原因：输入无效。'


def infer_method(rf, patch: dict | None = None) -> str | None:
    patch = patch or {}
    method = patch.get('method_code') or patch.get('calculation_method')
    if method:
        return method
    method = getattr(rf, 'method_code', None) or getattr(rf, 'calculation_method', None)
    if method:
        return method
    drop = patch['power_drop_kw'] if 'power_drop_kw' in patch else getattr(rf, 'power_drop_kw', None)
    frac = patch['saving_fraction'] if 'saving_fraction' in patch else getattr(rf, 'saving_fraction', None)
    inputs = patch['model_inputs'] if 'model_inputs' in patch else getattr(rf, 'model_inputs', None)
    if inputs and isinstance(inputs, dict) and any(
        k in inputs for k in ('eta_pump_before', 'q_m3_h', 'head_m', 'eta_fan_before', 'q_m3_s', 'delta_p_pa')
    ):
        if any(k in inputs for k in ('eta_pump_before', 'q_m3_h', 'head_m')):
            return 'pump_hydraulic'
        if any(k in inputs for k in ('eta_fan_before', 'q_m3_s', 'delta_p_pa')):
            return 'fan_pressure_flow'
    if drop is not None and frac is None:
        return 'power_drop_hours'
    if frac is not None and drop is None:
        return 'baseline_fraction'
    return None


def _device_of(case, device_id: str | None):
    if not device_id:
        return None
    return next((d for d in (case.devices or []) if d.id == device_id), None)


def check_power_drop(device, drop) -> None:
    if drop is None or device is None or getattr(device, 'power_kw', None) is None:
        return
    try:
        if float(drop) > float(device.power_kw):
            raise ChangeRejected(MSG_POWER, field='power_drop_kw')
    except (TypeError, ValueError) as e:
        if isinstance(e, ChangeRejected):
            raise
        raise ChangeRejected(MSG_NAN, field='power_drop_kw')


def invalidate_selection(sel, reason: str) -> None:
    sel.active = False
    sel.applied = False
    sel.superseded = True
    if not getattr(sel, 'invalidated_reason', None):
        try:
            sel.invalidated_reason = reason
        except Exception:
            pass


def snapshot_measure_refs(case, measure_id: str) -> dict:
    refs = [
        (s.model_dump() if hasattr(s, 'model_dump') else dict(s))
        for s in (case.reference_selections or [])
        if (getattr(s, 'measure_id', None) or None) == measure_id
    ]
    pvs = [
        dict(x) for x in (getattr(case, 'parameter_values', None) or [])
        if (x.get('measure_id') or None) == measure_id
    ]
    return {'_refs': refs, '_pvs': pvs}


def restore_measure_refs(case, measure_id: str, snap: dict | None) -> None:
    if not snap:
        return
    extra_refs = snap.get('_refs') if isinstance(snap, dict) else None
    extra_pvs = snap.get('_pvs') if isinstance(snap, dict) else None
    if extra_refs is None:
        return
    from ..models.schema import ReferenceSelection
    kept = [s for s in (case.reference_selections or []) if (getattr(s, 'measure_id', None) or None) != measure_id]
    for raw in extra_refs:
        data = dict(raw)
        data['measure_id'] = measure_id
        kept.append(ReferenceSelection(**{k: v for k, v in data.items() if k in ReferenceSelection.model_fields}))
    case.reference_selections = kept
    if extra_pvs is not None:
        others = [x for x in (getattr(case, 'parameter_values', None) or []) if (x.get('measure_id') or None) != measure_id]
        case.parameter_values = others + [dict(x) for x in extra_pvs]


def supersede_bindings_for_manual_edit(case, measure, changed_keys: list[str], *, reason: str = '用户手动修改参数') -> None:
    if not changed_keys:
        return
    for sel in (case.reference_selections or []):
        if (getattr(sel, 'measure_id', None) or None) not in (None, measure.id):
            continue
        if (getattr(sel, 'measure_id', None) or None) is None and getattr(sel, 'applicable_device_id', None) not in (None, measure.device_id):
            continue
        if (getattr(sel, 'measure_id', None) or None) != measure.id:
            # 无 measure_id 的绑定只在同设备且参数匹配时处理
            if getattr(sel, 'measure_id', None):
                continue
        key = getattr(sel, 'parameter_key', None)
        if key not in changed_keys:
            continue
        if not (getattr(sel, 'applied', False) or getattr(sel, 'active', False)):
            continue
        invalidate_selection(sel, reason)
    pvs = []
    for x in (getattr(case, 'parameter_values', None) or []):
        same = (
            (x.get('measure_id') or None) in (None, measure.id)
            and x.get('key') in changed_keys
            and x.get('applied')
        )
        if same and (x.get('measure_id') == measure.id or x.get('device_id') == measure.device_id):
            continue
        pvs.append(x)
    case.parameter_values = pvs


def on_measure_changed(case, measure, *, old_device_id, old_method, new_device_id, new_method) -> None:
    if old_device_id == new_device_id and old_method == new_method:
        return
    reason = '措施设备或计算方法已变更'
    for sel in (case.reference_selections or []):
        if (getattr(sel, 'measure_id', None) or None) not in (None, measure.id):
            continue
        if (getattr(sel, 'measure_id', None) or None) is None:
            if getattr(sel, 'applicable_device_id', None) not in (old_device_id, new_device_id):
                continue
        if not (getattr(sel, 'applied', False) or getattr(sel, 'active', False)):
            continue
        mismatch = False
        if old_device_id != new_device_id:
            if (getattr(sel, 'applicable_device_id', None) or None) != (new_device_id or None):
                mismatch = True
        if old_method != new_method:
            sm = getattr(sel, 'method_code', None) or None
            if sm not in (None, new_method):
                mismatch = True
        if mismatch:
            invalidate_selection(sel, reason)


def _archive_parameter_values(case, *, measure_ids=None, device_id=None, reason: str) -> None:
    """保留历史参数记录，但去掉当前 applied/active 标记。"""
    measure_ids = set(measure_ids or [])
    pvs = []
    for x in (getattr(case, 'parameter_values', None) or []):
        item = dict(x) if isinstance(x, dict) else x
        mid = item.get('measure_id') if isinstance(item, dict) else None
        did = item.get('device_id') if isinstance(item, dict) else None
        hit = (mid in measure_ids) or (device_id and did == device_id)
        if hit and isinstance(item, dict) and (item.get('applied') or item.get('active')):
            item = dict(item)
            item['applied'] = False
            item['active'] = False
            item['invalidated_reason'] = item.get('invalidated_reason') or reason
        pvs.append(item)
    case.parameter_values = pvs


def on_measure_deleted(case, measure_id: str) -> None:
    for sel in (case.reference_selections or []):
        if (getattr(sel, 'measure_id', None) or None) != measure_id:
            continue
        if getattr(sel, 'applied', False) or getattr(sel, 'active', False):
            invalidate_selection(sel, '措施已删除')
            sel.revoked = True
    _archive_parameter_values(case, measure_ids={measure_id}, reason='措施已删除')


def on_device_deleted(case, device_id: str, measure_ids=None) -> None:
    """设备软删除：全部关联措施与设备级参考失效，保留历史但不保持当前有效。"""
    mids = set(measure_ids or [])
    if not mids:
        mids = {rf.id for rf in (getattr(case, 'removed_retrofits', None) or []) if rf.device_id == device_id}
        mids |= {rf.id for rf in (getattr(case, 'retrofits', None) or []) if rf.device_id == device_id}
    for mid in mids:
        on_measure_deleted(case, mid)
    for sel in (case.reference_selections or []):
        sel_dev = getattr(sel, 'applicable_device_id', None) or getattr(sel, 'device_id', None)
        if sel_dev != device_id and (getattr(sel, 'measure_id', None) or None) not in mids:
            continue
        if getattr(sel, 'applied', False) or getattr(sel, 'active', False):
            invalidate_selection(sel, '设备已删除')
            sel.revoked = True
    _archive_parameter_values(case, measure_ids=mids, device_id=device_id, reason='设备已删除')


def retire_tariff_quote(case, reason: str) -> None:
    """手动改价/清空后，旧参考电价报价改为历史状态，不再显示正在采用。"""
    sc = getattr(case, 'scenario', None)
    if sc is not None:
        sc.use_jinhui_tariff_reference = False
    qc = getattr(case, 'quote_choice', None)
    if qc is None:
        return
    usage = getattr(qc, 'quote_usage_mode', None)
    kind = getattr(qc, 'quote_kind', None)
    ref_field = getattr(qc, 'reference_field', None) if hasattr(qc, 'reference_field') else None
    if not (
        usage == 'tariff_reference'
        or ref_field == 'energy_price'
        or kind == 'jinhui_reference'
    ):
        return
    qc.active = False
    qc.superseded = True
    qc.invalidated_reason = reason
    qc.invalidated_at = getattr(qc, 'invalidated_at', None) or datetime.now(timezone.utc).isoformat(timespec='seconds')
    qc.applied_to_investment = False


def retire_device_quotes(case, device_id: str, reason: str) -> None:
    """设备类型变化或删除后，旧报价失效/待复核，不作为确定投资。"""
    qc = getattr(case, 'quote_choice', None)
    if qc is None:
        return
    qd = getattr(qc, 'device_id', None)
    # 绑定该设备，或未绑定设备但当前作为确定采用的报价，均需复核
    if qd and qd != device_id:
        return
    qc.active = False
    qc.superseded = True
    qc.invalidated_reason = reason
    qc.invalidated_at = getattr(qc, 'invalidated_at', None) or datetime.now(timezone.utc).isoformat(timespec='seconds')
    qc.compatibility_result = 'needs_review'
    qc.applied_to_investment = False
    qc.compatibility_verified_by_user = False


def on_device_type_changed(case, device) -> None:
    reason = '设备类型已更改。原计算方法不再适用，请重新选择测算方法。'
    for rf in (case.retrofits or []):
        if rf.device_id != device.id:
            continue
        method = getattr(rf, 'method_code', None) or getattr(rf, 'calculation_method', None)
        if method and not equip_cat.method_compatible_with_device(device, method):
            for sel in (case.reference_selections or []):
                if (getattr(sel, 'measure_id', None) or None) not in (None, rf.id):
                    continue
                if (getattr(sel, 'applicable_device_id', None) or None) not in (None, device.id) and (
                    getattr(sel, 'measure_id', None) or None
                ) != rf.id:
                    continue
                if getattr(sel, 'applied', False) or getattr(sel, 'active', False):
                    invalidate_selection(sel, reason)
        else:
            # 类型变化后资料对设备类型不再适用
            for sel in (case.reference_selections or []):
                if (getattr(sel, 'measure_id', None) or None) != rf.id and (
                    getattr(sel, 'applicable_device_id', None) or None
                ) != device.id:
                    continue
                if getattr(sel, 'applied', False) or getattr(sel, 'active', False):
                    sm = getattr(sel, 'method_code', None)
                    if sm and not equip_cat.method_compatible_with_device(device, sm):
                        invalidate_selection(sel, reason)


def _changed_parameter_keys(old_rf, new_data: dict) -> list[str]:
    keys: list[str] = []
    old_mi = dict(getattr(old_rf, 'model_inputs', None) or {})
    new_mi = dict(new_data.get('model_inputs') or {})
    for k in set(old_mi) | set(new_mi):
        if old_mi.get(k) != new_mi.get(k):
            keys.append(k)
    if getattr(old_rf, 'power_drop_kw', None) != new_data.get('power_drop_kw'):
        keys.append('power_drop_kw')
    if getattr(old_rf, 'saving_fraction', None) != new_data.get('saving_fraction'):
        keys.append('saving_fraction')
    if getattr(old_rf, 'investment_cny', None) != new_data.get('investment_cny'):
        keys.append('investment_cny')
    if getattr(old_rf, 'annual_maintenance_cny', None) != new_data.get('annual_maintenance_cny'):
        keys.append('annual_maintenance_cny')
    return keys


def _build_updated_payload(case, rf, patch: dict) -> dict:
    data = rf.model_dump()
    if patch.get('name'):
        data['name'] = patch['name']
    if patch.get('device_id'):
        if not any(d.id == patch['device_id'] for d in (case.devices or [])):
            raise ChangeRejected(f"目标设备不存在: {patch['device_id']}", field='device_id')
        data['device_id'] = patch['device_id']
        dev = _device_of(case, patch['device_id'])
        data['target_device'] = patch.get('target_device') or (dev.name if dev else data.get('target_device'))
    method = infer_method(rf, patch)
    model_inputs = patch['model_inputs'] if 'model_inputs' in patch else data.get('model_inputs')
    if method:
        dev = _device_of(case, data.get('device_id'))
        if dev is not None and not equip_cat.method_compatible_with_device(dev, method):
            raise ChangeRejected(MSG_METHOD_DEVICE, field='method_code')
        data['method_code'] = method
        data['calculation_method'] = method
    if method in PROFESSIONAL_METHOD_CODES:
        if not model_inputs:
            raise ChangeRejected(f'专业方法 {method} 需要提供 model_inputs 工况数据', field='model_inputs')
        trial = phys.run_method(method, dict(model_inputs))
        if trial.get('status') != 'ok':
            raise ChangeRejected(trial.get('error') or '专业模型输入无效', field='model_inputs')
        data['model_inputs'] = dict(model_inputs)
        data['power_drop_kw'] = None
        data['saving_fraction'] = None
    elif method == 'power_drop_hours':
        if 'power_drop_kw' in patch:
            data['power_drop_kw'] = patch.get('power_drop_kw')
        data['saving_fraction'] = None
        data['model_inputs'] = None
    elif method == 'baseline_fraction':
        if 'saving_fraction' in patch:
            data['saving_fraction'] = patch.get('saving_fraction')
        data['power_drop_kw'] = None
        data['model_inputs'] = None
    else:
        if 'power_drop_kw' in patch:
            data['power_drop_kw'] = patch.get('power_drop_kw')
        if 'saving_fraction' in patch:
            data['saving_fraction'] = patch.get('saving_fraction')
        if 'model_inputs' in patch:
            data['model_inputs'] = patch.get('model_inputs')
    if 'investment_cny' in patch:
        data['investment_cny'] = patch.get('investment_cny')
    if 'annual_maintenance_cny' in patch:
        data['annual_maintenance_cny'] = patch.get('annual_maintenance_cny')
    if 'note' in patch:
        data['note'] = patch.get('note') or ''
    if 'source' in patch:
        data['source'] = patch.get('source')
    dev = _device_of(case, data.get('device_id'))
    check_power_drop(dev, data.get('power_drop_kw'))
    return data


def _measure_computable(case, measure_id: str) -> bool:
    from .compute import recalculate_case
    probe = deepcopy(case)
    if probe.scenario is not None:
        probe.scenario.selected_measure_id = measure_id
    try:
        results = recalculate_case(probe)
    except Exception:
        return False
    sa = results.get('selected_assessment') or {}
    if sa.get('measure_id') != measure_id:
        return False
    if sa.get('saved_kwh') is None:
        return False
    return sa.get('status') not in ('insufficient_data', 'invalid_input')


def validate_and_apply_retrofit_update(case, rid: str, patch: dict, *, draft_only: bool = False) -> Any:
    """在案例副本上应用补丁：校验→试算→来源状态更新。调用方负责原子提交。"""
    rf = next((x for x in (case.retrofits or []) if x.id == rid), None)
    if rf is None:
        raise ChangeRejected(f'措施不存在: {rid}')
    expected = patch.get('measure_revision')
    if expected is None:
        expected = patch.get('expected_revision')
    if expected is not None:
        try:
            if int(expected) != int(getattr(rf, 'revision', 1) or 1):
                raise ChangeRejected(MSG_STALE, status=409)
        except (TypeError, ValueError) as e:
            if isinstance(e, ChangeRejected):
                raise
            raise ChangeRejected(MSG_STALE, status=409)

    was_computable = _measure_computable(case, rid)
    old_device_id = rf.device_id
    old_method = getattr(rf, 'method_code', None) or getattr(rf, 'calculation_method', None) or infer_method(rf, {})
    old_dump = rf.model_dump()

    try:
        payload = _build_updated_payload(case, rf, patch)
    except ChangeRejected:
        raise
    except Exception as e:
        raise ChangeRejected(_friendly_validation(e))

    # 用户改动已披露的关键参数时，身份改为用户修改/假设
    tech_changed = any(
        old_dump.get(k) != payload.get(k)
        for k in ('power_drop_kw', 'saving_fraction', 'model_inputs', 'investment_cny', 'annual_maintenance_cny')
    )
    if tech_changed and (payload.get('data_type') in (None, '披露', '文献', '实测') or patch.get('mark_user_edit', True)):
        if old_dump.get('data_type') == '披露' and tech_changed:
            payload['data_type'] = '用户提供·未核验'
            src = payload.get('source') or ''
            if '用户修改' not in src:
                payload['source'] = (src + '（用户修改/假设）').strip('（）') if False else (
                    (src + '（用户修改/假设）') if src else '用户修改/假设'
                )
            payload['provenance_label'] = '用户修改/假设'

    if draft_only:
        payload['lifecycle_status'] = 'draft'
    else:
        if payload.get('lifecycle_status') == 'draft':
            payload['lifecycle_status'] = 'active'

    payload['previous_snapshot'] = {**old_dump, **snapshot_measure_refs(case, rid)}
    payload['revision'] = int(getattr(rf, 'revision', 1) or 1) + 1
    payload.pop('_refs', None)
    payload.pop('_pvs', None)

    try:
        updated = RetrofitProject.model_validate(payload)
    except ValidationError as e:
        raise ChangeRejected(_friendly_validation(e))
    except Exception as e:
        raise ChangeRejected(_friendly_validation(e))

    idx = case.retrofits.index(rf)
    case.retrofits[idx] = updated

    if patch.get('select'):
        case.scenario.selected_measure_id = rid

    changed_keys = _changed_parameter_keys(rf, payload)
    # 方法或数值变化后同步来源
    supersede_bindings_for_manual_edit(case, updated, changed_keys)
    on_measure_changed(
        case, updated,
        old_device_id=old_device_id,
        old_method=old_method,
        new_device_id=updated.device_id,
        new_method=updated.method_code or updated.calculation_method,
    )

    if not draft_only and was_computable and not _measure_computable(case, rid):
        raise ChangeRejected(MSG_UNCOMPUTABLE)
    return updated


def prepare_copied_measure(src_rf, new_id: str) -> dict:
    data = src_rf.model_dump()
    data['id'] = new_id
    data['name'] = (src_rf.name or '措施') + '（副本）'
    data['revision'] = 1
    data['previous_snapshot'] = None
    data['copied_from_id'] = src_rf.id
    data['data_type'] = '用户提供·未核验'
    data['source'] = MSG_COPIED
    data['provenance_label'] = MSG_COPIED
    data['lifecycle_status'] = None
    return data


def add_copied_provenance(case, new_rf, src_rf) -> None:
    from ..models.schema import ReferenceSelection
    import uuid
    mi = dict(getattr(new_rf, 'model_inputs', None) or {})
    # 只记一条总览，避免伪造已核验来源
    case.reference_selections = list(case.reference_selections or []) + [
        ReferenceSelection(
            source_id=f'copied-from:{src_rf.id}',
            verified_by_user=False,
            applicable_device_id=new_rf.device_id,
            method_code=new_rf.method_code or new_rf.calculation_method,
            selected_at=None,
            choice='use_own',
            title=MSG_COPIED,
            applied=False,
            parameter_key=None,
            parameter_value=None,
            apply_intent='bookmark',
            measure_id=new_rf.id,
            value_kind=MSG_COPIED,
            binding_id=str(uuid.uuid4()),
            active=False,
            revoked=False,
            superseded=False,
        )
    ]
    _ = mi
