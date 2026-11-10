"""碳核算模块：电网电力相关排放估算。"""
from .equipment import GAN_SU_GRID_FACTOR_2023_KGCO2_PER_KWH

DEFAULT_FACTOR = GAN_SU_GRID_FACTOR_2023_KGCO2_PER_KWH

FACTOR_META = {
    'value': DEFAULT_FACTOR,
    'unit': 'kgCO2/kWh',
    'source': '生态环境部2023年电力因子附件·甘肃平均',
    'source_name': '2023年甘肃平均电力因子/参考情景',
    'year': 2023,
    'boundary': '仅电网购电相关估算，不含工艺过程排放；对外仅称电力相关减排情景',
    'data_type': '文献',
}


def factor_meta_for(factor_kg_per_kwh: float, user_override: bool = False) -> dict:
    """按实际使用的因子动态生成来源说明。"""
    try:
        f = float(factor_kg_per_kwh)
    except (TypeError, ValueError):
        f = DEFAULT_FACTOR
    is_default = abs(f - DEFAULT_FACTOR) < 1e-9 and not user_override
    if is_default:
        return {
            **FACTOR_META,
            'value': f,
            'label': '2023年甘肃平均电力因子/参考情景',
            'user_override': False,
        }
    return {
        'value': f,
        'unit': 'kgCO2/kWh',
        'source': '用户提供',
        'source_name': '用户提供',
        'year': None,
        'boundary': FACTOR_META['boundary'],
        'data_type': '假设',
        'label': '用户提供',
        'user_override': True,
    }


def estimate_grid_co2_t(
    kwh: float,
    factor_kg_per_kwh: float = GAN_SU_GRID_FACTOR_2023_KGCO2_PER_KWH,
    user_override: bool = False,
) -> dict:
    if kwh < 0 or factor_kg_per_kwh < 0:
        raise ValueError('kWh 与排放因子不得为负')
    t = round(kwh * factor_kg_per_kwh / 1000, 3)
    meta = factor_meta_for(factor_kg_per_kwh, user_override=user_override)
    return {
        'co2_t': t,
        'inputs': {'kwh': kwh, 'factor_kg_per_kwh': factor_kg_per_kwh},
        'formula': 'tCO2 = kWh × factor_kgCO2/kWh / 1000',
        'param_sources': meta,
        'unit': 'tCO2',
        'boundary': meta['boundary'],
        'data_type': '模型估算',
        'is_actual_reduction': False,
    }
