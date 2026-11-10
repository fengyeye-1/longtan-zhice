"""公开采购报价目录 + 金徽基础参考 + 项目私有询价。

规则：
- 候选报价 ≠ 确认成交价，禁止默认写入任意设备投资
- 切换报价只改成本/回本相关，不改 saved_kwh / co2
- user_quote 仅本项目可见
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..calc.equipment import COST_INTENSITY_CNY_PER_KWH, PUBLISHED_POWER_DROP_KW, PUBLISHED_PUMP_RATE

VFD_SOURCE = 'https://www.gsei.com.cn/html/1337/2024-03-22/content-492836.html'
PUMP_SOURCE = 'https://manage.gsei.com.cn/html/1337/2025-09-22/content-622794.html'

PUBLIC_PROJECTS: list[dict[str, Any]] = [
    {
        'project_id': 'proj-vfd-2024-baiyin',
        'buyer': '白银有色选矿公司',
        'project_title': '变频器采购（2024）',
        'region': '甘肃·白银',
        'announced_at': '2024-03-22',
        'equipment_category': '变频器',
        'tender_scope': '变频器设备采购（项目总报价）',
        'specification': '公告未披露逐台型号/功率分解',
        'quantity': None,
        'source_url': VFD_SOURCE,
        'source_status': 'public_announcement',
        'tax_scope': 'unknown',
        'installation_scope': 'unknown',
        'notes': '候选价≠确认成交价；不可默认写入任意球磨机投资',
    },
    {
        'project_id': 'proj-pump-2025-baiyin',
        'buyer': '白银有色深部矿业公司',
        'project_title': '水泵采购（2025）',
        'region': '甘肃·白银',
        'announced_at': '2025-09-22',
        'equipment_category': '水泵',
        'tender_scope': '水泵设备采购（项目总报价）',
        'specification': '公告未披露流量/扬程/台数分解',
        'quantity': None,
        'source_url': PUMP_SOURCE,
        'source_status': 'public_announcement',
        'tax_scope': 'unknown',
        'installation_scope': 'unknown',
        'notes': '候选价≠确认成交价；不可冒充单台泵售价',
    },
    {
        'project_id': 'proj-jinhui-reference',
        'buyer': '金徽矿业（公开示范基础公司）',
        'project_title': '金徽基础参考参数（非采购中标）',
        'region': '甘肃',
        'announced_at': '2025',
        'equipment_category': '参考',
        'tender_scope': '公开案例参考强度与历史技改披露',
        'specification': '公司账面电费强度；球磨历史降功率；泵相对节电',
        'quantity': None,
        'source_url': '',
        'source_status': 'demo_reference',
        'tax_scope': 'n/a',
        'installation_scope': 'n/a',
        'notes': '仅作参考；须用户显式勾选，禁止静默套用',
    },
]

PUBLIC_QUOTES: list[dict[str, Any]] = [
    {
        'quote_id': 'q-vfd-wolong',
        'project_id': 'proj-vfd-2024-baiyin',
        'bidder_full_name': '卧龙电气驱动集团股份有限公司',
        'rank': 1,
        'total_quote_cny': 138000,
        'currency': 'CNY',
        'quote_kind': 'public_bid',
        'source_url': VFD_SOURCE,
        'unit_price_cny': None,
        'quantity': None,
        'spec_verified': False,
        'scope_verified': False,
        'procurement_status': 'candidate',
        'label': '变频器·卧龙电气 138,000',
        'equipment_category': '变频器',
    },
    {
        'quote_id': 'q-vfd-lixiang',
        'project_id': 'proj-vfd-2024-baiyin',
        'bidder_full_name': '白银理想电子科技有限公司',
        'rank': 2,
        'total_quote_cny': 164705,
        'currency': 'CNY',
        'quote_kind': 'public_bid',
        'source_url': VFD_SOURCE,
        'unit_price_cny': None,
        'quantity': None,
        'spec_verified': False,
        'scope_verified': False,
        'procurement_status': 'candidate',
        'label': '变频器·白银理想 164,705',
        'equipment_category': '变频器',
    },
    {
        'quote_id': 'q-vfd-dongdian',
        'project_id': 'proj-vfd-2024-baiyin',
        'bidder_full_name': '甘肃东电机械加工有限公司',
        'rank': 3,
        'total_quote_cny': 224700,
        'currency': 'CNY',
        'quote_kind': 'public_bid',
        'source_url': VFD_SOURCE,
        'unit_price_cny': None,
        'quantity': None,
        'spec_verified': False,
        'scope_verified': False,
        'procurement_status': 'candidate',
        'label': '变频器·甘肃东电 224,700',
        'equipment_category': '变频器',
    },
    {
        'quote_id': 'q-pump-zhongli',
        'project_id': 'proj-pump-2025-baiyin',
        'bidder_full_name': '郑州中力泵阀制造有限公司',
        'rank': 1,
        'total_quote_cny': 472070,
        'currency': 'CNY',
        'quote_kind': 'public_bid',
        'source_url': PUMP_SOURCE,
        'unit_price_cny': None,
        'quantity': None,
        'spec_verified': False,
        'scope_verified': False,
        'procurement_status': 'candidate',
        'label': '水泵·郑州中力 472,070',
        'equipment_category': '水泵',
    },
    {
        'quote_id': 'q-pump-gongye',
        'project_id': 'proj-pump-2025-baiyin',
        'bidder_full_name': '郑州市工业泵厂',
        'rank': 2,
        'total_quote_cny': 520800,
        'currency': 'CNY',
        'quote_kind': 'public_bid',
        'source_url': PUMP_SOURCE,
        'unit_price_cny': None,
        'quantity': None,
        'spec_verified': False,
        'scope_verified': False,
        'procurement_status': 'candidate',
        'label': '水泵·郑州工业泵厂 520,800',
        'equipment_category': '水泵',
    },
    {
        'quote_id': 'q-pump-naipu',
        'project_id': 'proj-pump-2025-baiyin',
        'bidder_full_name': '湖南耐普泵业股份有限公司',
        'rank': 3,
        'total_quote_cny': 580000,
        'currency': 'CNY',
        'quote_kind': 'public_bid',
        'source_url': PUMP_SOURCE,
        'unit_price_cny': None,
        'quantity': None,
        'spec_verified': False,
        'scope_verified': False,
        'procurement_status': 'candidate',
        'label': '水泵·湖南耐普 580,000',
        'equipment_category': '水泵',
    },
    # 金徽基础参考（用户额外要求：可显式选择基础示范公司相关报价/电价强度）
    {
        'quote_id': 'q-jinhui-tariff',
        'project_id': 'proj-jinhui-reference',
        'bidder_full_name': '金徽矿业（基础示范公司）',
        'rank': 0,
        'total_quote_cny': None,
        'currency': 'CNY',
        'quote_kind': 'jinhui_reference',
        'source_url': '',
        'unit_price_cny': COST_INTENSITY_CNY_PER_KWH,
        'quantity': None,
        'spec_verified': True,
        'scope_verified': False,
        'procurement_status': 'reference',
        'label': f'金徽参考电价强度 {COST_INTENSITY_CNY_PER_KWH:.6f} 元/kWh',
        'equipment_category': '参考',
        'reference_field': 'energy_price',
        'reference_value': COST_INTENSITY_CNY_PER_KWH,
        'reference_unit': '元/kWh',
        'notes': '2025年报账面电费/用电量；并非本企业实际电价',
    },
    {
        'quote_id': 'q-jinhui-mill-200kw',
        'project_id': 'proj-jinhui-reference',
        'bidder_full_name': '金徽矿业（基础示范公司）',
        'rank': 0,
        'total_quote_cny': 1_000_000,
        'currency': 'CNY',
        'quote_kind': 'jinhui_reference',
        'source_url': '',
        'unit_price_cny': None,
        'quantity': None,
        'spec_verified': False,
        'scope_verified': False,
        'procurement_status': 'reference',
        'label': f'金徽球磨历史技改参考（{int(PUBLISHED_POWER_DROP_KW)} kW / 假设投资100万）',
        'equipment_category': '变频器',
        'reference_field': 'investment_cny',
        'reference_value': 1_000_000,
        'reference_unit': '元',
        'notes': '历史技改披露功率降幅可另选；投资为演示假设',
        'tech_hint': {'power_drop_kw': PUBLISHED_POWER_DROP_KW},
    },
    {
        'quote_id': 'q-jinhui-pump-rate',
        'project_id': 'proj-jinhui-reference',
        'bidder_full_name': '金徽矿业（基础示范公司）',
        'rank': 0,
        'total_quote_cny': None,
        'currency': 'CNY',
        'quote_kind': 'jinhui_reference',
        'source_url': '',
        'unit_price_cny': None,
        'quantity': None,
        'spec_verified': False,
        'scope_verified': False,
        'procurement_status': 'reference',
        'label': f'金徽泵历史节电比例参考 {PUBLISHED_PUMP_RATE*100:.2f}%',
        'equipment_category': '水泵',
        'reference_field': 'saving_fraction',
        'reference_value': PUBLISHED_PUMP_RATE,
        'reference_unit': '比例',
        'notes': '仅同类技术情景参考，不能作用于全公司电量',
    },
]


def list_projects(equipment_category: str | None = None) -> list[dict]:
    rows = deepcopy(PUBLIC_PROJECTS)
    if equipment_category:
        cat = equipment_category.strip()
        rows = [p for p in rows if p['equipment_category'] == cat or p['equipment_category'] == '参考']
    return rows


def list_quotes(
    project_id: str | None = None,
    equipment_category: str | None = None,
    user_quotes: list[dict] | None = None,
) -> list[dict]:
    rows = deepcopy(PUBLIC_QUOTES)
    if project_id:
        rows = [q for q in rows if q['project_id'] == project_id]
    if equipment_category:
        cat = equipment_category.strip()
        rows = [q for q in rows if q.get('equipment_category') == cat or q.get('quote_kind') == 'jinhui_reference']
    if user_quotes:
        for uq in user_quotes:
            item = deepcopy(uq)
            item.setdefault('quote_kind', 'user_quote')
            item.setdefault('procurement_status', 'user_private')
            rows.append(item)
    return rows


def get_quote(quote_id: str, user_quotes: list[dict] | None = None) -> dict | None:
    for q in PUBLIC_QUOTES:
        if q['quote_id'] == quote_id:
            return deepcopy(q)
    for uq in user_quotes or []:
        if uq.get('quote_id') == quote_id:
            return deepcopy(uq)
    return None


def get_project(project_id: str) -> dict | None:
    for p in PUBLIC_PROJECTS:
        if p['project_id'] == project_id:
            return deepcopy(p)
    return None


def assess_compatibility(
    quote: dict,
    *,
    device_type: str | None = None,
    measure_tech: str | None = None,
    confirm_same_scope: bool = False,
    confirm_spec_and_scope: bool = False,
) -> dict:
    """适配判断。缺型号/数量/范围核对方不得标记已核实匹配；采购价≠完整技改投资。"""
    kind = quote.get('quote_kind')
    cat = quote.get('equipment_category') or ''
    dt = (device_type or '').strip()
    tech = (measure_tech or '').strip()
    verified = bool(confirm_spec_and_scope and quote.get('spec_verified') and quote.get('scope_verified'))

    # 金徽电价参考不是供应商采购条目的“投资”
    if kind == 'jinhui_reference' and quote.get('reference_field') == 'energy_price':
        return {
            'compatibility_result': 'compatible' if confirm_same_scope else 'needs_adaptation',
            'message': '金徽参考电价：仅作电费试算，不是供应商采购报价',
            'can_apply_investment': False,
            'can_apply_tariff': True,
            'is_tariff_reference': True,
            'verified_match': False,
        }

    if kind == 'jinhui_reference':
        return {
            'compatibility_result': 'compatible' if confirm_same_scope else 'needs_adaptation',
            'message': '金徽基础参考：勾选确认后可作参考情景，须标注「参考、并非本企业实际」',
            'can_apply_investment': False,  # 参考投资也只能敏感性，不能当完整核实投资
            'can_apply_tariff': False,
            'can_price_sensitivity': quote.get('total_quote_cny') is not None,
            'verified_match': False,
            'is_tariff_reference': False,
        }

    if kind == 'user_quote':
        return {
            'compatibility_result': 'compatible' if (confirm_same_scope and confirm_spec_and_scope) else 'needs_adaptation',
            'message': '企业自行提供的询价：仅本项目可见；未核验不称已核实匹配',
            'can_apply_investment': bool(confirm_same_scope and confirm_spec_and_scope and quote.get('total_quote_cny') is not None),
            'can_apply_tariff': False,
            'can_price_sensitivity': quote.get('total_quote_cny') is not None,
            'verified_match': bool(confirm_spec_and_scope),
            'is_tariff_reference': False,
        }

    # 公开采购：类别明显不符 → incompatible
    if dt and cat and cat not in ('参考',):
        millish = ('磨' in dt) or dt in ('通用', '球磨机')
        pumpish = '泵' in dt
        if cat == '变频器' and not millish and not ('变频' in dt or '电机' in dt):
            if not pumpish:  # 泵也可能配变频，标 needs_adaptation
                pass
        if cat == '变频器' and tech == 'process':
            # 变频器采购包 ≠ 碎磨工艺改进完整投资
            return {
                'compatibility_result': 'needs_adaptation',
                'message': '变频器采购报价不能直接作为碎磨工艺改进的完整技改投资；可做价格敏感性假设实验',
                'can_apply_investment': False,
                'can_apply_tariff': False,
                'can_price_sensitivity': quote.get('total_quote_cny') is not None,
                'verified_match': False,
                'is_tariff_reference': False,
                'scope_mismatch_reason': 'quote_is_equipment_procurement_not_full_process_retrofit',
            }
        if cat == '水泵' and not pumpish and not millish:
            return {
                'compatibility_result': 'incompatible',
                'message': '这份报价对应的设备规格/类别不同，暂时不能直接套用',
                'can_apply_investment': False,
                'can_apply_tariff': False,
                'can_price_sensitivity': False,
                'verified_match': False,
                'is_tariff_reference': False,
            }
        if cat == '水泵' and millish and tech == 'process':
            return {
                'compatibility_result': 'incompatible',
                'message': '水泵采购报价与碎磨工艺改进不是同一类改造，不能直接套用',
                'can_apply_investment': False,
                'can_apply_tariff': False,
                'can_price_sensitivity': False,
                'verified_match': False,
                'is_tariff_reference': False,
            }

    # 缺型号/台数/范围核实 → 不得已核实匹配；不可当完整投资
    if not quote.get('spec_verified') or not quote.get('scope_verified') or not confirm_spec_and_scope:
        return {
            'compatibility_result': 'needs_adaptation' if not confirm_same_scope else 'needs_adaptation',
            'message': '先核对型号、台数与服务范围；未核实前只能查看或做价格敏感性假设，不能写成确定投资回收期',
            'can_apply_investment': False,
            'can_apply_tariff': False,
            'can_price_sensitivity': quote.get('total_quote_cny') is not None,
            'verified_match': False,
            'is_tariff_reference': False,
        }

    return {
        'compatibility_result': 'compatible',
        'message': '可用于参考情景试算（仍非确认成交价；完整技改还需安装/停产/税费等）',
        'can_apply_investment': False,  # 公开候选价默认仍不可当完整技改总投资
        'can_apply_tariff': False,
        'can_price_sensitivity': quote.get('total_quote_cny') is not None,
        'verified_match': True,
        'is_tariff_reference': False,
    }



def catalog_payload(user_quotes: list[dict] | None = None) -> dict:
    return {
        'projects': list_projects(),
        'quotes': list_quotes(user_quotes=user_quotes),
        'disclaimer': '甘肃采购项目公开报价；候选价≠确认成交价；切换报价不改变节电与减排。',
        'jinhui_reference_tariff': COST_INTENSITY_CNY_PER_KWH,
        'ui_labels': {
            'entry': '看看有哪些采购报价',
            'choose': '这次想参考哪家公司的报价？',
            'check': '先核对这份报价是不是同一类设备',
            'mismatch': '这份报价对应的设备规格不同，暂时不能直接套用',
            'trial': '按这份历史报价试算（仅供比较）',
            'tax_unknown': '报价是否包含安装和税费还不清楚',
            'add_mine': '添加我拿到的供应商报价',
            'empty': '还没有找到适合这台设备的公开报价，你可以上传询价单',
            'jinhui': '采用金徽基础示范公司参考',
        },
    }
