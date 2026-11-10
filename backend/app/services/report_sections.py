"""分模块报告：完整 / 一页结论 / 自选。主结果只认 selected_assessment。

P0-04：自选章节时 HTML/CSV/TXT 等价，只含所选（保留必要报告头与核算范围）。
CSV 的 schemes/methods/gaps/sources 必须有真实内容，不能只有表头。
"""
from __future__ import annotations

from typing import Any

from .compute import build_report_text, full_recalculate, sanitize_csv_cell, render_csv_rows

PRESETS: dict[str, list[str]] = {
    'full': ['summary', 'energy', 'carbon', 'roi', 'quotes', 'schemes', 'methods', 'gaps', 'sources'],
    'one_page': ['summary', 'energy', 'carbon', 'roi', 'methods'],
    'custom': [],
}

MODULE_META = {
    'summary': {'title': '一页摘要内容'},
    'energy': {'title': '节电测算'},
    'carbon': {'title': '电力相关减排'},
    'roi': {'title': '投资与回本'},
    'quotes': {'title': '采购报价参考'},
    'schemes': {'title': '小时情景对照'},
    'methods': {'title': '方法与假设'},
    'gaps': {'title': '缺口与限制'},
    'sources': {'title': '来源'},
}



def _active_binding_map(case: dict, measure_id: str | None, device_id: str | None) -> dict:
    out = {}
    for sel in case.get('reference_selections') or []:
        if not (sel.get('active') or sel.get('applied')):
            continue
        if sel.get('revoked') or sel.get('superseded'):
            continue
        if measure_id and (sel.get('measure_id') or None) not in (None, measure_id):
            continue
        if device_id and (sel.get('applicable_device_id') or None) not in (None, device_id):
            continue
        key = sel.get('parameter_key')
        if key:
            out[key] = sel
    return out


def collect_parameter_identity_rows(data: dict, sa: dict | None = None) -> list[dict]:
    """本次计算实际采用的参数身份表。"""
    sa = sa or _sa(data)
    case = data.get('case') or {}
    st = sa.get('source_trace') or {}
    measure_id = sa.get('measure_id')
    device_id = sa.get('device_id')
    bindings = _active_binding_map(case, measure_id, device_id)
    rows = []
    mi = st.get('model_inputs') or {}
    if not isinstance(mi, dict):
        mi = {}
    from . import equipment_catalog as equip_cat
    method = sa.get('calculation_method') or st.get('calculation_method')
    meta = dict((equip_cat.METHOD_META.get(method) or {}))
    field_meta = {f.get('key'): f for f in (meta.get('fields') or []) if f.get('key')}

    def _add(key, value, unit, default_kind='用户输入'):
        if value is None or value == '':
            return
        sel = bindings.get(key)
        if sel:
            kind = sel.get('value_kind') or default_kind
            title = sel.get('title') or sel.get('source_id') or ''
            locator = sel.get('excerpt_locator') or ''
            source = f"{title} {locator}".strip()
            status = '本次采用'
        else:
            kind = default_kind
            # 金徽年报不得充当用户填写效率的直接证据
            if key.startswith('eta_') or key == 'saving_fraction':
                kind = default_kind if default_kind != '披露' else '用户假设'
            source = '当前措施填写'
            status = '本次采用'
        label = (field_meta.get(key) or {}).get('label') or key
        unit = unit or (field_meta.get(key) or {}).get('unit') or ''
        rows.append({
            'parameter': label,
            'key': key,
            'value': value,
            'unit': unit,
            'identity': kind,
            'source': source,
            'status': status,
        })

    for key, val in mi.items():
        _add(key, val, (field_meta.get(key) or {}).get('unit') or '')
    if st.get('power_drop_kw') is not None:
        _add('power_drop_kw', st.get('power_drop_kw'), 'kW')
    if st.get('saving_fraction') is not None:
        _add('saving_fraction', st.get('saving_fraction'), '0-1')
    if sa.get('investment_cny') is not None:
        _add('investment_cny', sa.get('investment_cny'), '元', '用户输入')
    return rows


def format_parameter_identity_lines(data: dict, sa: dict | None = None) -> list[str]:
    rows = collect_parameter_identity_rows(data, sa)
    lines = [
        '【本次使用的参数及依据】',
        '以下为本次计算实际使用的数据。其他历史资料不等于本次采用的参数来源。',
        '参数名称｜本次取值｜单位｜数据身份｜来源标题/原文位置｜应用状态',
    ]
    if not rows:
        lines.append('（当前测算未登记可追溯的工况参数明细）')
    for r in rows:
        lines.append(
            f"{r['parameter']}｜{r['value']}｜{r['unit']}｜{r['identity']}｜{r['source']}｜{r['status']}"
        )
        # 便于验收检索 eta_pump_before=0.50
        lines.append(f"{r['key']}={r['value']}")
    lines.append('')
    return lines


def resolve_sections(preset: str, sections: list[str] | None = None) -> list[str]:
    preset = (preset or 'full').strip().lower()
    if preset == 'custom':
        wanted = [s for s in (sections or []) if s in MODULE_META]
        return wanted
    return list(PRESETS.get(preset, PRESETS['full']))


def _sa(payload: dict) -> dict:
    return (
        payload.get('selected_assessment')
        or (payload.get('assessment_snapshot') or {}).get('selected_assessment')
        or {}
    )


def module_status(payload: dict, section: str) -> dict[str, Any]:
    sa = _sa(payload)
    snap = payload.get('assessment_snapshot') or {}
    if section == 'energy':
        if sa.get('saved_kwh') is None:
            return {
                'status': 'blocked',
                'reason': sa.get('waiting_message') or '还缺计算节电所需的条件',
            }
        return {'status': 'ok'}
    if section == 'carbon':
        if sa.get('co2_reduction_t') is None:
            return {
                'status': 'blocked',
                'reason': sa.get('waiting_message') or '还缺节电量或排放换算依据',
            }
        return {'status': 'ok'}
    if section == 'roi':
        if sa.get('annual_cost_saving_cny') is None:
            return {
                'status': 'partial',
                'reason': sa.get('cost_note') or '节电和减碳已经算好了。填入每度电的费用，就能继续算省多少钱。',
            }
        if sa.get('simple_payback_years') is None:
            return {
                'status': 'partial',
                'reason': sa.get('payback_message') or '这项改造要花多少钱还没确定，回本时间暂时不能算。',
            }
        return {'status': 'ok'}
    if section == 'quotes':
        qc = snap.get('quote_choice')
        hist = snap.get('historical_quote_choice')
        raw = (payload.get('case') or {}).get('quote_choice')
        if qc:
            return {'status': 'ok'}
        if hist or (raw and (raw.get('superseded') or raw.get('active') is False)):
            return {'status': 'partial', 'reason': '旧参考报价已失效，不再作为当前采用'}
        return {'status': 'partial', 'reason': '尚未选择参考报价'}
    if section == 'summary':
        if sa.get('saved_kwh') is None:
            return {'status': 'partial', 'reason': sa.get('waiting_message') or '摘要条件不足'}
        return {'status': 'ok'}
    return {'status': 'ok'}


def build_modular_report(
    *,
    preset: str = 'full',
    sections: list[str] | None = None,
    payload: dict | None = None,
) -> dict[str, Any]:
    data = payload or full_recalculate()
    selected = resolve_sections(preset, sections)
    blocked = []
    partial = []
    for s in selected:
        st = module_status(data, s)
        if st.get('status') == 'blocked':
            blocked.append({'section': s, **st})
        elif st.get('status') == 'partial':
            partial.append({'section': s, **st})
    text = build_report_text(data, sections=selected)
    snap = data.get('assessment_snapshot') or {}
    case = data.get('case') or {}
    evidence_min = {
        'case_name': snap.get('case_name') or case.get('name'),
        'case_id': snap.get('case_id') or case.get('case_id'),
        'selected_measure_id': snap.get('selected_measure_id'),
        'calculated_at': snap.get('calculated_at') or data.get('calculated_at'),
    }
    return {
        'preset': preset,
        'sections': selected,
        'text': text,
        'blocked_sections': blocked,
        'partial_sections': partial,
        'payload': data,
        'snapshot_id': snap.get('snapshot_id'),
        'evidence_min': evidence_min,
    }


def _method_registry(sa: dict) -> dict:
    from . import equipment_catalog as equip_cat
    method = sa.get('calculation_method') or (sa.get('source_trace') or {}).get('calculation_method')
    return dict((equip_cat.METHOD_META.get(method) or {}))


def _assumption_kind(text: str) -> str:
    s = str(text or '')
    if any(k in s for k in ('安全', '风险', '未核验', '尚未核实', '待核实')):
        return '风险提示'
    if any(k in s for k in ('参考假设', '演示用', '默认')):
        return '假设'
    return '假设'


def _append_methods_rows(rows: list, data: dict, sa: dict) -> None:
    rows.append(['【方法与假设】', '', '', ''])
    rows.append(['模型', sa.get('model_label') or '', '', ''])
    rows.append(['计算方法代码（审计）', sa.get('calculation_method') or '', '', ''])
    if sa.get('formula'):
        rows.append(['公式', sa.get('formula'), '', ''])
    meta = _method_registry(sa)
    st = sa.get('source_trace') or {}
    mi = st.get('model_inputs') or {}
    if not isinstance(mi, dict):
        mi = {}
    fields = list(meta.get('fields') or [])
    required = list(meta.get('required_inputs') or [])
    seen_keys = set()
    for f in fields:
        key = f.get('key')
        if not key:
            continue
        seen_keys.add(key)
        val = mi.get(key)
        origin = '工况输入'
        if f.get('default') is not None and (val is None or val == f.get('default')):
            origin = '参考假设'
        if val is None or val == '':
            rows.append([f.get('label') or key, '待填写', f.get('unit') or '', '本模型字段'])
        else:
            rows.append([f.get('label') or key, val, f.get('unit') or '', origin])
    for key in required:
        if key in seen_keys:
            continue
        val = mi.get(key)
        if val is None or val == '':
            # 仅登记当前模型需要的字段
            rows.append([key, '待填写', '', '本模型字段'])
        else:
            rows.append([key, val, '', '工况输入'])
    assumptions = (sa.get('energy') or {}).get('assumptions') or sa.get('assumptions') or []
    for a in assumptions:
        rows.append(['假设', a, '', _assumption_kind(a)])


def _append_gaps_rows(rows: list, data: dict, sa: dict) -> None:
    rows.append(['【缺口与限制】', '', '', ''])
    gaps = []
    if sa.get('waiting_message'):
        gaps.append(sa.get('waiting_message'))
    if sa.get('cost_note') and sa.get('annual_cost_saving_cny') is None:
        gaps.append(sa.get('cost_note'))
    if sa.get('payback_message') and sa.get('simple_payback_years') is None:
        gaps.append(sa.get('payback_message'))
    for w in sa.get('consistency_warnings') or []:
        gaps.append(w)
    if sa.get('recommendation_block_reason'):
        gaps.append(sa.get('recommendation_block_reason'))
    for item in data.get('data_prep') or []:
        if item.get('status') in ('missing', 'unknown', '待补充', '缺口'):
            gaps.append(f"{item.get('label') or item.get('key')}: {item.get('message') or item.get('status')}")
    if sa.get('safety_unverified') or sa.get('safety_note'):
        note = sa.get('safety_note') or sa.get('recommendation_block_reason') or '安全条件待核实'
        gaps.append(note)
    # 去重保序
    dedup = []
    seen = set()
    for g in gaps:
        key = str(g).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        dedup.append(g)
    gaps = dedup
    if not gaps:
        gaps.append('当前已选章节未见明显缺口；正式实施前请结合现场数据复核。')
    for g in gaps:
        kind = '风险提示' if any(k in str(g) for k in ('安全', '未核', '待核实', '小时')) else '限制'
        rows.append(['缺口', g, '', kind])


def _append_sources_rows(rows: list, data: dict, sa: dict) -> None:
    rows.append(['【本次使用的参数及依据】', '', '', ''])
    rows.append(['以下为本次计算实际使用的数据。其他历史资料不等于本次采用的参数来源。', '', '', ''])
    rows.append(['参数名称', '本次取值', '单位', '数据身份｜来源｜状态'])
    for r in collect_parameter_identity_rows(data, sa):
        rows.append([
            r['parameter'],
            r['value'],
            r['unit'],
            f"{r['identity']}｜{r['source']}｜{r['status']}",
        ])
        rows.append([r['key'], r['value'], r['unit'], r['identity']])
    rows.append(['【相关研究资料】', '', '', ''])
    fm = data.get('factor_meta') or {}
    if fm:
        rows.append([
            '排放因子',
            fm.get('value') if fm.get('value') is not None else (data.get('case') or {}).get('scenario', {}).get('emission_factor'),
            'kgCO₂/kWh',
            fm.get('label') or fm.get('source_name') or '',
        ])
        if fm.get('source_url'):
            rows.append(['因子链接', fm.get('source_url'), '', fm.get('year') or ''])
    case = data.get('case') or {}
    for sel in case.get('reference_selections') or []:
        is_current = (sel.get('active') or sel.get('applied')) and not sel.get('revoked') and not sel.get('superseded')
        if is_current:
            continue
        status = '已撤销' if sel.get('revoked') else ('仅收藏' if sel.get('apply_intent') == 'bookmark' else '历史记录')
        if sel.get('superseded'):
            status = '历史记录/已覆盖'
        rows.append([
            sel.get('title') or sel.get('source_id') or '参考资料',
            status,
            sel.get('value_kind') or sel.get('choice') or '',
            '不等于本次采用的参数来源',
        ])
    # 默认公开源提示
    rows.append([
        '边界说明',
        case.get('boundary') or (data.get('assessment_snapshot') or {}).get('boundary') or '电力相关减排情景',
        '',
        '结果用于改造前期参考，正式实施前请结合现场数据复核。',
    ])
    st = sa.get('source_trace') or {}
    tariff = st.get('tariff') or (data.get('assessment_snapshot') or {}).get('tariff') or {}
    if tariff.get('value') is not None:
        rows.append(['电价来源', tariff.get('value'), '元/kWh', tariff.get('source') or ''])


def _append_schemes_rows(rows: list, data: dict, sa: dict) -> None:
    rows.append(['【小时情景对照】', '', '', ''])
    tech = sa.get('technology_type') or ''
    schemes_rows = data.get('schemes') or []
    if tech.startswith('professional_') or tech in ('pump', 'generic_fraction') or not schemes_rows:
        rows.append([
            '说明',
            '当前选中措施不适用「小时对照」表（该表仅用于功率差×有效小时模型）。'
            if (tech in ('pump', 'generic_fraction') or tech.startswith('professional_') or not schemes_rows)
            else '无情景行',
            '',
            sa.get('model_label') or tech or '',
        ])
        return
    rows.append(['有效小时', '节电kWh', '减排tCO2', '节费元'])
    for s in schemes_rows:
        rows.append([
            s.get('hours'),
            s.get('annual_electricity_saved_kwh'),
            s.get('scenario_grid_co2_saved_t'),
            s.get('scenario_electricity_cost_saved_cny'),
        ])


def _append_quotes_rows(rows: list, data: dict) -> None:
    snap = data.get('assessment_snapshot') or {}
    qc = snap.get('quote_choice') or (data.get('case') or {}).get('quote_choice') or {}
    rows.append(['【采购报价参考】', '', '', ''])
    if not qc:
        rows.append(['参考报价', '尚未选择', '', ''])
        return
    rows.append(['参考报价企业', qc.get('bidder_full_name') or qc.get('label') or '未选', '', ''])
    rows.append(['参考报价金额', qc.get('total_quote_cny'), '元', qc.get('quote_kind') or ''])
    rows.append(['项目', qc.get('project_title') or '', '', ''])
    rows.append(['报价用途', qc.get('quote_usage_mode') or '', '', ''])
    rows.append(['适配结果', qc.get('compatibility_result') or '', '', ''])
    rows.append(['说明', '历史候选总价不是本企业的当前采购价', '', '标签'])


def build_modular_csv(payload: dict | None = None, sections: list[str] | None = None) -> str:
    data = payload or full_recalculate()
    sa = _sa(data)
    snap = data.get('assessment_snapshot') or {}
    fm = data.get('factor_meta') or {}
    wanted = list(sections or resolve_sections('full'))
    wanted_set = set(wanted)

    rows: list[list] = [['字段', '数值', '单位', '标记']]
    # 必要报告头（任何章节都保留）
    rows.append(['案例', (data.get('case') or {}).get('name') or '', '', ''])
    rows.append(['核算范围', snap.get('boundary') or '电力相关减排情景', '', '边界'])
    rows.append(['所选章节', ','.join(wanted), '', ''])

    business = {'summary', 'energy', 'carbon', 'roi'}
    if wanted_set & business:
        rows.append(['设备', sa.get('device_name') or '', '', ''])
        rows.append(['措施名称', sa.get('measure_name') or snap.get('selected_measure_name') or '', '', ''])
    if 'energy' in wanted_set or 'summary' in wanted_set:
        rows.append(['情景节电', sa.get('saved_kwh', ''), 'kWh', '模型估算'])
    if 'carbon' in wanted_set or 'summary' in wanted_set:
        rows.append(['电力相关减排', sa.get('co2_reduction_t', ''), 'tCO2', '模型估算'])
        rows.append(['因子说明', fm.get('label') or fm.get('source_name') or '', '', ''])
    if 'roi' in wanted_set or 'summary' in wanted_set:
        cost = sa.get('annual_cost_saving_cny')
        rows.append([
            '情景电费节省',
            cost if cost is not None else (sa.get('cost_note') or '待补电价'),
            '元',
            '参考电价' if sa.get('tariff_is_reference') else '模型估算',
        ])
        rows.append([
            '回收期',
            sa.get('simple_payback_years') if sa.get('simple_payback_years') is not None else sa.get('payback_message', ''),
            '年',
            '',
        ])
        tariff = (sa.get('source_trace') or {}).get('tariff') or snap.get('tariff') or {}
        rows.append(['电价', tariff.get('value'), '元/kWh', tariff.get('source') or ''])
        rows.append(['投资', sa.get('investment_cny'), '元', ''])

    if 'quotes' in wanted_set:
        _append_quotes_rows(rows, data)
    if 'schemes' in wanted_set:
        _append_schemes_rows(rows, data, sa)
    if 'methods' in wanted_set:
        _append_methods_rows(rows, data, sa)
    if 'gaps' in wanted_set:
        _append_gaps_rows(rows, data, sa)
    if 'sources' in wanted_set:
        _append_sources_rows(rows, data, sa)

    # 仅 quotes 时加明确说明，避免夹带主结果误解
    if wanted_set == {'quotes'} or (wanted_set <= {'quotes', 'sources', 'methods', 'gaps'} and not (wanted_set & business)):
        rows.append([
            '报告说明',
            '这份报告只比较报价与适用条件，不包含无关设备的减碳结论。' if 'quotes' in wanted_set and not (wanted_set & business) else '按所选章节导出',
            '',
            '',
        ])

    return render_csv_rows(rows)


def build_modular_html(payload: dict | None, sections: list[str] | None, text: str) -> str:
    """按所选章节渲染 HTML；不再总是夹带固定「一页结论」。"""
    import html as _html
    data = payload or {}
    case = data.get('case') or {}
    sa = _sa(data)
    snap = data.get('assessment_snapshot') or {}
    name = _html.escape(str(case.get('name') or '未命名企业'))
    device = _html.escape(str(sa.get('device_name') or '尚待补充'))
    measure = _html.escape(str(sa.get('measure_name') or '尚待补充'))
    wanted = list(sections or [])
    wanted_set = set(wanted)
    business = {'summary', 'energy', 'carbon', 'roi'}

    def _num(v, unit=''):
        if v is None:
            return '尚待补充'
        try:
            return f'{float(v):,.4g}{unit}'
        except Exception:
            return '尚待补充'

    body = _html.escape(text or '').replace('\n', '<br>\n')
    sec = _html.escape(', '.join(wanted))

    parts: list[str] = [
        '<!DOCTYPE html>',
        '<html lang="zh-CN">',
        '<head>',
        '<meta charset="utf-8"/>',
        '<title>陇碳智策 企业评估材料</title>',
        '<style>',
        'body{font-family:"Microsoft YaHei","PingFang SC","Segoe UI",sans-serif;'
        'color:#111;background:#fff;margin:24px;line-height:1.6;}',
        'h1{font-size:22px;margin:0 0 8px} h2{font-size:16px;border-bottom:1px solid #E5E5E5;padding-bottom:4px}',
        '.muted{color:#555;font-size:13px} .card{border:1px solid #E5E5E5;padding:12px 16px;margin:12px 0}',
        'table{border-collapse:collapse;width:100%;margin:8px 0} th,td{border:1px solid #E5E5E5;padding:6px 8px;text-align:left}',
        '@media print{body{margin:12mm}}',
        '</style>',
        '</head>',
        '<body>',
        '  <h1>设备节能与电力相关减排评估</h1>',
        f'  <p class="muted">封面与企业信息 · 章节：{sec}</p>',
        '  <div class="card">',
        f'    <strong>企业/案例：</strong>{name}<br/>',
        f'    <strong>核算范围：</strong>{_html.escape(str(snap.get("boundary") or "电力相关减排情景"))}',
        '  </div>',
    ]

    if wanted_set & business:
        parts += [
            '  <div class="card">',
            f'    <strong>设备：</strong>{device}　<strong>措施：</strong>{measure}',
            '  </div>',
        ]

    # 仅当用户明确选了 summary / one_page 业务摘要时才渲染「一页结论」
    if 'summary' in wanted_set:
        pb = sa.get('simple_payback_years')
        payback_msg = sa.get('payback_message') or '尚待补充'
        pb_cell = _num(pb, ' 年') if pb is not None else _html.escape(str(payback_msg))
        parts += [
            '  <h2>一页结论</h2>',
            '  <table>',
            f'    <tr><th>预计一年省电</th><td>{_num(sa.get("saved_kwh"), " kWh")}</td></tr>',
            f'    <tr><th>预计电力相关减排</th><td>{_num(sa.get("co2_reduction_t"), " tCO2")}</td></tr>',
            f'    <tr><th>简单回收期</th><td>{pb_cell}</td></tr>',
            '  </table>',
        ]
    elif wanted_set & {'energy', 'carbon', 'roi'}:
        parts.append('  <h2>所选测算结果</h2>')
        parts.append('  <table>')
        if 'energy' in wanted_set:
            parts.append(f'    <tr><th>预计一年省电</th><td>{_num(sa.get("saved_kwh"), " kWh")}</td></tr>')
        if 'carbon' in wanted_set:
            parts.append(f'    <tr><th>预计电力相关减排</th><td>{_num(sa.get("co2_reduction_t"), " tCO2")}</td></tr>')
        if 'roi' in wanted_set:
            cost = sa.get('annual_cost_saving_cny')
            cost_cell = _num(cost, ' 元') if cost is not None else _html.escape(str(sa.get('cost_note') or '尚待补充'))
            parts.append(f'    <tr><th>预计电费节省</th><td>{cost_cell}</td></tr>')
        parts.append('  </table>')

    if 'quotes' in wanted_set:
        qc = snap.get('quote_choice')
        hist = snap.get('historical_quote_choice')
        raw = case.get('quote_choice') or {}
        if not hist and raw and (raw.get('superseded') or raw.get('active') is False):
            hist = raw
            if qc and (qc.get('superseded') or qc.get('active') is False):
                qc = None
        parts.append('  <h2>采购报价参考</h2>')
        if qc and not (qc.get('superseded') or qc.get('active') is False):
            parts += [
                '  <table>',
                f'    <tr><th>当前采用</th><td>{_html.escape(str(qc.get("bidder_full_name") or qc.get("label") or ""))}</td></tr>',
                f'    <tr><th>金额</th><td>{_html.escape(str(qc.get("total_quote_cny") or ""))} 元</td></tr>',
                f'    <tr><th>用途</th><td>{_html.escape(str(qc.get("quote_usage_mode") or ""))}</td></tr>',
                '  </table>',
                '  <p class="muted">这份报告只比较报价与适用条件，不包含无关设备的减碳结论。</p>',
            ]
        elif hist:
            reason = hist.get('invalidated_reason') or '用户改价或条件变化'
            when = hist.get('invalidated_at') or hist.get('superseded_at') or hist.get('updated_at') or ''
            parts += [
                '  <p>曾经引用·已失效，不再作为当前采用。</p>',
                '  <table>',
                f'    <tr><th>历史引用</th><td>{_html.escape(str(hist.get("bidder_full_name") or hist.get("label") or "参考报价"))}</td></tr>',
                f'    <tr><th>失效原因</th><td>{_html.escape(str(reason))}</td></tr>',
                f'    <tr><th>失效时间</th><td>{_html.escape(str(when) or "—")}</td></tr>',
                '  </table>',
            ]
        else:
            parts.append('  <p class="muted">尚未选择参考报价。</p>')

    parts += [
        '  <h2>报告正文（可审计 TXT 结构）</h2>',
        f'  <div class="card" style="white-space:pre-wrap;font-size:13px">{body}</div>',
        '  <p class="muted">结果用于改造前期参考，正式实施前请结合现场数据复核。没有数据的分项显示「尚待补充」，不作虚构精确值。</p>',
        '</body>',
        '</html>',
    ]
    return '\n'.join(parts)
