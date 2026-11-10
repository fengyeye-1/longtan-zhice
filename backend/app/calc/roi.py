"""投资回报分析模块。"""


def simple_payback(investment_cny: float, annual_net_saving_cny: float) -> dict:
    if investment_cny < 0:
        raise ValueError('投资不得为负')
    if annual_net_saving_cny < 0:
        raise ValueError('年净节省不得为负（可先核对维护费）')
    years = None
    if annual_net_saving_cny > 0:
        years = round(investment_cny / annual_net_saving_cny, 2)
    return {
        'simple_payback_years': years,
        'inputs': {
            'investment_cny': investment_cny,
            'annual_net_saving_cny': annual_net_saving_cny,
        },
        'formula': '回收期 = 投资 / (年电费节省 - 年维护费)',
        'unit': '年',
        'data_type': '模型估算',
        'boundary': '简单回收期，不计折现与税率',
        'is_actual_saving': False,
    }
