"""节能改造情景模拟。"""
from .equipment import mill_scenario, pump_scenario, PUBLISHED_PUMP_RATE, MAX_ANNUAL_HOURS_LEAP


def device_power_saving(
    mode: str,
    *,
    hours: float | None = None,
    power_drop_kw: float | None = None,
    baseline_power_kw: float | None = None,
    baseline_device_kwh: float | None = None,
    pump_rate: float = PUBLISHED_PUMP_RATE,
    cost_intensity: float | None = None,
    emission_factor: float | None = None,
    investment_cny: float | None = None,
    annual_maintenance_cny: float | None = 0,
) -> dict:
    """mode: mill | pump。返回带 status 的结构，不把缺失当成 0。"""
    if mode == 'mill':
        kwargs = {}
        if cost_intensity is not None:
            kwargs['tariff_cny_per_kwh'] = cost_intensity
        if emission_factor is not None:
            kwargs['ef_kgco2_per_kwh'] = emission_factor
        r = mill_scenario(
            delta_power_kw=power_drop_kw,
            hours=hours,
            baseline_power_kw=baseline_power_kw,
            investment_cny=investment_cny,
            annual_extra_om_cny=annual_maintenance_cny,
            **kwargs,
        )
        return r
    if mode == 'pump':
        kwargs = {}
        if emission_factor is not None:
            kwargs['ef_kgco2_per_kwh'] = emission_factor
        if cost_intensity is not None:
            kwargs['tariff_cny_per_kwh'] = cost_intensity
        return pump_scenario(
            pump_baseline_kwh=baseline_device_kwh,
            saving_fraction=pump_rate,
            **kwargs,
        )
    raise ValueError(f'不支持的模式: {mode}')


def aggregate_enterprise_energy(devices: list[dict]) -> dict:
    """汇总页面设备列表的情景电耗。功率或小时为 None 时该设备不计入（不是按 0）。"""
    total_kwh = 0.0
    details = []
    skipped = []
    for d in devices:
        raw_power = d.get('power_kw', None)
        raw_hours = d.get('hours', None)
        if raw_power is None or raw_hours is None:
            skipped.append({
                'name': d.get('name'),
                'reason': '功率或小时未提供',
            })
            continue
        power = float(raw_power)
        hours = float(raw_hours)
        if power < 0 or hours < 0:
            raise ValueError(f"设备 {d.get('name')} 功率/小时不得为负")
        if hours > MAX_ANNUAL_HOURS_LEAP:
            raise ValueError(f"设备 {d.get('name')} 有效小时不可行（上限 {MAX_ANNUAL_HOURS_LEAP}，含闰年）")
        kwh = power * hours
        total_kwh += kwh
        details.append({
            'name': d.get('name'),
            'power_kw': power,
            'hours': hours,
            'scenario_kwh': round(kwh, 2),
            'data_type': d.get('data_type', '假设'),
        })
    return {
        'device_count': len(devices),
        'included_count': len(details),
        'scenario_total_kwh': round(total_kwh, 2) if details else None,
        'devices': details,
        'skipped': skipped,
        'formula': '设备情景电耗 = Σ(功率kW × 小时)；未提供功率/小时的设备不计入',
        'boundary': '页面设备清单情景合计，≠ 企业年报披露总量',
        'data_type': '模型估算',
        'is_company_disclosed_total': False,
    }
