"""三个标准 Excel 模板。"""
from pathlib import Path
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[2] / 'data' / 'templates'
ROOT.mkdir(parents=True, exist_ok=True)

STANDARD_FIELDS = {
    'energy': ['年份', '工业用电量_万kWh', '电力费用_万元', '柴油使用量_万升', '柴油费用_万元', '边界说明', '来源URL'],
    'device': ['设备名称', '设备ID', '设备类型', '功率_kW', '年运行小时', '效率', '基准年电耗_kWh', '产量_t', '数据来源', '数据类型'],
    'retrofit': ['项目名称', '目标设备', '设备ID', '功率降低_kW', '节电比例', '计算方法', '投资_元', '年维护费_元', '数据来源', '备注'],
}


def build_templates() -> dict[str, str]:
    paths = {}
    specs = {
        'energy': ('企业能源活动模板.xlsx', STANDARD_FIELDS['energy'], [
            [2025, 1000, 450, 10, 60, '示例边界', 'https://example.com'],
        ]),
        'device': ('设备参数模板.xlsx', STANDARD_FIELDS['device'], [
            ['示例球磨机', '', '球磨', 500, 4000, 0.9, '', '', '用户假设', '假设'],
            ['示例泵', '', '泵', 50, 4000, '', 200000, '', '用户假设', '假设'],
        ]),
        'retrofit': ('技术改造项目模板.xlsx', STANDARD_FIELDS['retrofit'], [
            ['球磨优化', '示例球磨机', '', 200, '', 'power_drop_hours', 1000000, 0, '用户假设', '小时需另设'],
        ]),
    }
    for kind, (fname, headers, rows) in specs.items():
        wb = Workbook()
        ws = wb.active
        ws.title = kind
        ws.append(headers)
        for r in rows:
            ws.append(r)
        path = ROOT / fname
        wb.save(path)
        paths[kind] = str(path)
    return paths


def template_path(kind: str) -> Path:
    build_templates()
    mapping = {
        'energy': ROOT / '企业能源活动模板.xlsx',
        'device': ROOT / '设备参数模板.xlsx',
        'retrofit': ROOT / '技术改造项目模板.xlsx',
    }
    if kind not in mapping:
        raise KeyError('未知模板类型')
    return mapping[kind]
