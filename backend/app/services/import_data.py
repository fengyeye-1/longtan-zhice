"""CSV/XLSX 上传：预览、字段映射、校验；缺失保持 null。"""
from __future__ import annotations
import io
import time
import uuid
import zipfile
from collections import defaultdict
from typing import Any

import pandas as pd

from ..models.schema import (
    EnergyYear, Device, RetrofitProject, FieldMapping, ScenarioParams,
    FIELD_ABS_LIMITS, _strict_int_year, sanitize_user_url,
)
from .store import get_case, replace_active_working_copy, JINHUI_ID
from ..models.schema import CaseState, ScenarioParams
from .templates_xlsx import STANDARD_FIELDS

REQUIRED = {
    'energy': ['年份', '工业用电量_万kWh'],
    'device': ['设备名称', '功率_kW'],
    'retrofit': ['项目名称', '目标设备'],
}

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5MB
MAX_UPLOAD_ROWS = 10_000
MAX_UPLOAD_COLS = 128
MAX_XLSX_UNCOMPRESSED = 40 * 1024 * 1024  # 40MB
MAX_XLSX_RATIO = 200
MAX_XLSX_MEMBER_FILES = 256
UPLOAD_RATE_LIMIT = 20
UPLOAD_RATE_WINDOW_SEC = 60
ALLOWED_EXT = ('.csv', '.xlsx')
_UPLOAD_HITS: dict[str, list[float]] = {}


def _is_blank(v: Any) -> bool:
    if v is None:
        return True
    s = str(v).strip()
    return s in ('', 'nan', 'None', 'NaN', 'NULL', 'null')


def _unique_prefixed_id(prefix: str, taken: set[str]) -> str:
    for _ in range(32):
        cid = f'{prefix}-{uuid.uuid4().hex}'
        if cid not in taken:
            taken.add(cid)
            return cid
    raise ValueError(f'无法生成唯一{prefix}标识，同项目出现编号冲突，请重试或整批重新导入')


def _opt_float(row, col, ndf) -> float | None:
    if col not in ndf.columns:
        return None
    if _is_blank(row.get(col)):
        return None
    return float(row[col])


def _invalidate_orphan_bindings(case) -> None:
    """替换设备/措施清单后，失效指向已不存在对象或不适用设备类型的参考绑定。"""
    from . import equipment_catalog as equip_cat
    from .change_validation import invalidate_selection

    dids = {d.id: d for d in (case.devices or [])}
    mids = {m.id for m in (case.retrofits or [])}
    sels = list(getattr(case, 'reference_selections', None) or [])
    for sel in sels:
        mid = getattr(sel, 'measure_id', None)
        did = getattr(sel, 'applicable_device_id', None)
        orphan = (mid and mid not in mids) or (did and did not in dids)
        incompatible = False
        if not orphan and did and did in dids:
            device = dids[did]
            method = getattr(sel, 'method_code', None)
            if method and not equip_cat.method_compatible_with_device(device, method):
                incompatible = True
        if (orphan or incompatible) and (getattr(sel, 'active', False) or getattr(sel, 'applied', False)):
            reason = (
                '设备类型已更改，原专业参考参数不再适用于当前设备'
                if incompatible else
                '设备或措施清单已替换，原引用绑定已失效'
            )
            try:
                invalidate_selection(sel, reason)
            except Exception:
                sel.active = False
                sel.applied = False
                sel.superseded = True
                sel.invalidated_reason = sel.invalidated_reason or reason
    case.reference_selections = sels


def check_upload_limits(content: bytes, filename: str) -> None:
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f'文件过大：上限 {MAX_UPLOAD_BYTES // (1024*1024)}MB')
    name = (filename or '').lower()
    if not any(name.endswith(ext) for ext in ALLOWED_EXT):
        raise ValueError('仅支持 CSV / XLSX 格式')


def check_upload_rate(key: str | None = None) -> None:
    """按会话/来源限制上传频率，避免异常工作簿拖垮服务。"""
    ident = key or 'anonymous'
    now = time.time()
    hits = [t for t in _UPLOAD_HITS.get(ident, []) if now - t < UPLOAD_RATE_WINDOW_SEC]
    if len(hits) >= UPLOAD_RATE_LIMIT:
        raise ValueError(f'上传过于频繁：{UPLOAD_RATE_WINDOW_SEC} 秒内最多 {UPLOAD_RATE_LIMIT} 次，请稍后再试')
    hits.append(now)
    _UPLOAD_HITS[ident] = hits
    # 防止字典无限增长
    if len(_UPLOAD_HITS) > 1000:
        stale = [k for k, ts in _UPLOAD_HITS.items() if not ts or now - ts[-1] > UPLOAD_RATE_WINDOW_SEC]
        for k in stale:
            _UPLOAD_HITS.pop(k, None)


def _check_xlsx_zip_bomb(content: bytes) -> None:
    bio = io.BytesIO(content)
    if not zipfile.is_zipfile(bio):
        raise ValueError('XLSX 文件损坏或不是有效工作簿')
    bio.seek(0)
    try:
        zf = zipfile.ZipFile(bio)
    except zipfile.BadZipFile:
        raise ValueError('XLSX 文件损坏或不是有效工作簿') from None
    with zf:
        names = zf.namelist()
        if len(names) > MAX_XLSX_MEMBER_FILES:
            raise ValueError(f'工作簿内部文件过多（上限 {MAX_XLSX_MEMBER_FILES}）')
        total_unc = 0
        total_comp = 0
        for info in zf.infolist():
            name = (info.filename or '').replace('\\', '/')
            if name.startswith('/') or '..' in name.split('/'):
                raise ValueError('XLSX 包含非法内部路径')
            if info.file_size < 0 or info.compress_size < 0:
                raise ValueError('XLSX 文件头无效')
            total_unc += int(info.file_size)
            total_comp += int(info.compress_size or 0)
            if info.file_size > MAX_XLSX_UNCOMPRESSED:
                raise ValueError(
                    f'工作簿单个内部文件解压后超过上限 {MAX_XLSX_UNCOMPRESSED // (1024*1024)}MB'
                )
        if total_unc > MAX_XLSX_UNCOMPRESSED:
            raise ValueError(
                f'工作簿解压后体积过大：上限 {MAX_XLSX_UNCOMPRESSED // (1024*1024)}MB'
            )
        if total_comp > 0 and total_unc > 2 * 1024 * 1024 and (total_unc / max(total_comp, 1)) > MAX_XLSX_RATIO:
            raise ValueError('工作簿压缩展开比过高，已拒绝解析以防资源耗尽')


def read_table(content: bytes, filename: str) -> pd.DataFrame:
    check_upload_limits(content, filename)
    name = filename.lower()
    bio = io.BytesIO(content)
    if name.endswith('.csv'):
        try:
            df = pd.read_csv(bio, encoding='utf-8-sig')
        except Exception:
            bio.seek(0)
            df = pd.read_csv(bio, encoding='gbk')
    elif name.endswith('.xlsx'):
        _check_xlsx_zip_bomb(content)
        df = pd.read_excel(bio, engine='openpyxl')
    elif name.endswith('.xls'):
        raise ValueError('已不再支持旧版 .xls，请另存为 .xlsx 或导出 CSV')
    else:
        raise ValueError('仅支持 CSV / XLSX')
    if df.shape[1] > MAX_UPLOAD_COLS:
        raise ValueError(f'列数过多：上限 {MAX_UPLOAD_COLS} 列，当前 {df.shape[1]} 列')
    if len(df) > MAX_UPLOAD_ROWS:
        raise ValueError(f'行数过多：上限 {MAX_UPLOAD_ROWS} 行，当前 {len(df)} 行')
    return df


def validate_mapping_list(mappings: list, columns: list[str] | None = None) -> list[str]:
    """预览、提交与版本恢复共用的确定性映射规则。"""
    errors: list[str] = []
    by_std: dict[str, list[str]] = {}
    by_src: dict[str, list[str]] = {}
    for m in mappings or []:
        if hasattr(m, 'standard_field'):
            std = str(getattr(m, 'standard_field') or '').strip()
            src = str(getattr(m, 'user_column') or '').strip()
        elif isinstance(m, dict):
            std = str(m.get('standard_field') or '').strip()
            src = str(m.get('user_column') or '').strip()
        else:
            continue
        if not std:
            continue
        if not src:
            errors.append(f'标准字段「{std}」的映射列名为空')
            continue
        by_std.setdefault(std, []).append(src)
        by_src.setdefault(src, []).append(std)
        if columns is not None and src not in columns:
            errors.append(f'映射列不存在: {src} → {std}')
    for std, srcs in by_std.items():
        if len(srcs) > 1:
            if len(srcs) == 2:
                errors.append(f'标准字段「{std}」被重复映射：列「{srcs[0]}」与列「{srcs[1]}」')
            else:
                joined = '、'.join(f'「{s}」' for s in srcs)
                errors.append(f'标准字段「{std}」被重复映射：{joined}')
    for src, stds in by_src.items():
        unique_stds = list(dict.fromkeys(stds))
        if len(unique_stds) > 1:
            errors.append(
                f'源列「{src}」被重复映射到多个标准字段：「{unique_stds[0]}」与「{unique_stds[1]}」'
            )
        elif len(stds) > 1:
            errors.append(f'源列「{src}」出现重复映射')
    return errors


def preview_dataframe(df: pd.DataFrame, kind: str | None = None) -> dict:
    cols = [str(c) for c in df.columns]
    preview_df = df.head(20)
    # 缺失率
    missing_rate = {}
    for c in cols:
        miss = int(df[c].isna().sum()) if c in df.columns else 0
        # 也把空字符串算缺失
        try:
            miss += int((df[c].astype(str).str.strip() == '').sum())
        except Exception:
            pass
        missing_rate[c] = round(miss / max(len(df), 1), 4)
    rows = preview_df.where(pd.notnull(preview_df), None).astype(object).to_dict(orient='records')
    # 转可 JSON 的
    clean_rows = []
    for r in rows:
        clean_rows.append({k: (None if (v is None or (isinstance(v, float) and pd.isna(v))) else v) for k, v in r.items()})
    std = list(STANDARD_FIELDS.get(kind, [])) if kind else sorted({c for v in STANDARD_FIELDS.values() for c in v})
    replace_hint = {
        'energy': '将替换：企业年度能源表与电费强度推算；不替换：设备清单与技改措施（除非一并上传）',
        'device': '将替换设备清单，不替换年度能源表。未填设备ID且名称唯一对应时保留原ID及已绑定技改；若填写了与现网不同的设备ID则按新设备处理、不继承旧技改；无法自动匹配的技改会进入回收站，可恢复，不会无声删除。',
        'retrofit': '将替换：技改措施列表；不替换：年度能源与设备清单',
    }
    return {
        'columns': cols,
        'row_count': int(len(df)),
        'preview_rows': clean_rows,
        'preview_limit': 20,
        'missing_rate': missing_rate,
        'suggested_mappings': _suggest(cols),
        'standard_fields': std,
        'will_replace': replace_hint.get(kind or '', ''),
        'limits': {'max_bytes': MAX_UPLOAD_BYTES, 'max_rows': MAX_UPLOAD_ROWS},
    }


def _suggest(cols: list[str]) -> list[dict]:
    flat = {c: c for kind in STANDARD_FIELDS.values() for c in kind}
    out = []
    for c in cols:
        if c in flat:
            out.append({'user_column': c, 'standard_field': c})
    return out


def validate_mapped(df: pd.DataFrame, kind: str, mappings: list[FieldMapping]) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    row_errors: list[dict] = []
    if df.empty:
        errors.append('文件无数据行')
        return {'ok': False, 'errors': errors, 'warnings': warnings, 'row_errors': row_errors}

    columns = [str(c) for c in df.columns]
    map_errors = validate_mapping_list(mappings, columns)
    if map_errors:
        errors.extend(map_errors)
        return {'ok': False, 'errors': errors, 'warnings': warnings, 'row_errors': row_errors}

    colmap = {}
    for m in mappings:
        std = str(getattr(m, 'standard_field', '') or '').strip()
        src = str(getattr(m, 'user_column', '') or '').strip()
        if std and src:
            colmap[std] = src
    for std in STANDARD_FIELDS[kind]:
        if std not in colmap and std in df.columns:
            colmap[std] = std

    for req in REQUIRED[kind]:
        if req not in colmap:
            errors.append(f'缺少必填标准字段映射: {req}')

    if errors:
        return {'ok': False, 'errors': errors, 'warnings': warnings, 'colmap': colmap, 'row_errors': row_errors}

    data = {}
    for std, user_c in colmap.items():
        if user_c not in df.columns:
            errors.append(f'映射列不存在: {user_c} → {std}')
            continue
        data[std] = df[user_c]
    if errors:
        return {'ok': False, 'errors': errors, 'warnings': warnings, 'row_errors': row_errors}

    ndf = pd.DataFrame(data)

    if kind == 'energy':
        seen_years: dict[int, int] = {}
        for idx, row in ndf.iterrows():
            try:
                y = _strict_int_year(row['年份'], '年份')
                if y in seen_years:
                    errors.append('年份存在重复值')
                    row_errors.append({'row': int(idx) + 2, 'field': '年份', 'error': '规范化后重复'})
                else:
                    seen_years[y] = int(idx)
            except Exception:
                y = None
            try:
                if y is None:
                    y = _strict_int_year(row['年份'], '年份')
                e = float(row['工业用电量_万kWh'])
                if not __import__('math').isfinite(e):
                    errors.append(f'{y}: 用电量不能为 NaN/Infinity')
                    row_errors.append({'row': int(idx) + 2, 'field': '工业用电量_万kWh', 'error': '非有限数'})
                elif e < 0:
                    errors.append(f'{y}: 用电量不得为负')
                    row_errors.append({'row': int(idx) + 2, 'field': '工业用电量_万kWh', 'error': '不得为负'})
                if e > 1e8:
                    warnings.append(f'{y}: 用电量异常偏大，请核对单位是否为万kWh')
            except Exception:
                errors.append('年份或用电量类型无效')
                row_errors.append({'row': int(idx) + 2, 'field': '年份/用电量', 'error': '类型无效'})
            for col, label in (
                ('电力费用_万元', '电力费用'),
                ('柴油使用量_万升', '柴油使用量'),
                ('柴油费用_万元', '柴油费用'),
            ):
                if col in ndf.columns and not _is_blank(row.get(col)):
                    try:
                        c = float(row[col])
                        if not __import__('math').isfinite(c):
                            errors.append(f'{label}不能为 NaN/Infinity')
                            row_errors.append({'row': int(idx) + 2, 'field': col, 'error': '非有限数'})
                        elif c < 0:
                            errors.append(f'{label}不得为负')
                            row_errors.append({'row': int(idx) + 2, 'field': col, 'error': '不得为负'})
                    except Exception:
                        errors.append(f'{label}类型无效')
                        row_errors.append({'row': int(idx) + 2, 'field': col, 'error': '类型无效'})

    if kind == 'device':
        if ndf['设备名称'].duplicated().any():
            warnings.append('设备名称存在重复（导入技改时须用设备ID，不可仅用名称绑定）')
        for idx, row in ndf.iterrows():
            name = row.get('设备名称')
            if _is_blank(name):
                errors.append('设备名称不能为空')
                row_errors.append({'row': int(idx) + 2, 'field': '设备名称', 'error': '不能为空'})
                name = ''
            try:
                p = float(row['功率_kW'])
                if not __import__('math').isfinite(p):
                    errors.append(f'{name}: 功率不能为 NaN/Infinity')
                    row_errors.append({'row': int(idx) + 2, 'field': '功率_kW', 'error': '非有限数'})
                elif p < 0:
                    errors.append(f'{name}: 功率不得为负')
                    row_errors.append({'row': int(idx) + 2, 'field': '功率_kW', 'error': '不得为负'})
                elif p > FIELD_ABS_LIMITS['power_kw']:
                    errors.append(f'{name}: 功率超出合理范围')
                    row_errors.append({'row': int(idx) + 2, 'field': '功率_kW', 'error': '超出合理范围'})
            except Exception:
                errors.append(f'{name}: 功率类型无效')
                row_errors.append({'row': int(idx) + 2, 'field': '功率_kW', 'error': '类型无效'})
                p = None
            hours_v = None
            if '年运行小时' in ndf.columns and not _is_blank(row.get('年运行小时')):
                try:
                    hours_v = float(row['年运行小时'])
                    if not __import__('math').isfinite(hours_v) or hours_v < 0 or hours_v > 8784:
                        errors.append(f'{name}: 年运行小时超出可行范围 0–8784')
                        row_errors.append({'row': int(idx) + 2, 'field': '年运行小时', 'error': '超出范围'})
                except Exception:
                    errors.append(f'{name}: 小时类型无效')
            if '效率' in ndf.columns and not _is_blank(row.get('效率')):
                try:
                    eff = float(row['效率'])
                    if not __import__('math').isfinite(eff) or not (0 <= eff <= 1):
                        errors.append(f'{name}: 效率须在 0–1')
                        row_errors.append({'row': int(idx) + 2, 'field': '效率', 'error': '须在0–1'})
                except Exception:
                    errors.append(f'{name}: 效率类型无效')
                    row_errors.append({'row': int(idx) + 2, 'field': '效率', 'error': '类型无效'})
            for col, label in (('基准年电耗_kWh', '基准年电耗'), ('产量_t', '产量')):
                if col in ndf.columns and not _is_blank(row.get(col)):
                    try:
                        v = float(row[col])
                        if not __import__('math').isfinite(v):
                            errors.append(f'{name}: {label}不能为 NaN/Infinity')
                            row_errors.append({'row': int(idx) + 2, 'field': col, 'error': '非有限数'})
                        elif v < 0:
                            errors.append(f'{name}: {label}不得为负')
                            row_errors.append({'row': int(idx) + 2, 'field': col, 'error': '不得为负'})
                    except Exception:
                        errors.append(f'{name}: {label}类型无效')
                        row_errors.append({'row': int(idx) + 2, 'field': col, 'error': '类型无效'})
            # P0-06：功率×小时 vs 基准年电耗一致性提示
            if (
                p is not None and p >= 0
                and hours_v is not None and hours_v >= 0
                and '基准年电耗_kWh' in ndf.columns and not _is_blank(row.get('基准年电耗_kWh'))
            ):
                try:
                    base_kwh = float(row['基准年电耗_kWh'])
                    cap = p * hours_v
                    if base_kwh > cap * 1.001:
                        warnings.append(
                            f'{name}: 设备功率、运行时间与全年用电量不一致'
                            f'（功率×小时≈{cap:.0f} kWh，基准年电耗={base_kwh:.0f} kWh），'
                            f'请检查是否混用了不同设备或统计范围'
                        )
                except Exception:
                    pass

    if kind == 'retrofit':
        for idx, row in ndf.iterrows():
            if _is_blank(row.get('项目名称')):
                errors.append('技改项目名称不能为空')
                row_errors.append({'row': int(idx) + 2, 'field': '项目名称', 'error': '不能为空'})
            has_drop = False
            has_frac = False
            if '功率降低_kW' in ndf.columns and not _is_blank(row.get('功率降低_kW')):
                raw = str(row.get('功率降低_kW')).strip()
                if raw.lower() in ('nan', 'inf', '-inf', '+inf'):
                    errors.append('功率降低不能为 NaN/Infinity')
                    row_errors.append({'row': int(idx) + 2, 'field': '功率降低_kW', 'error': '非有限数'})
                else:
                    try:
                        v = float(row['功率降低_kW'])
                        if not __import__('math').isfinite(v):
                            errors.append('功率降低不能为 NaN/Infinity')
                            row_errors.append({'row': int(idx) + 2, 'field': '功率降低_kW', 'error': '非有限数'})
                        elif v < 0:
                            errors.append('功率降低不得为负')
                        else:
                            has_drop = True
                    except Exception:
                        errors.append('功率降低类型无效')
            if '节电比例' in ndf.columns and not _is_blank(row.get('节电比例')):
                raw = str(row.get('节电比例')).strip()
                if raw.lower() in ('nan', 'inf', '-inf', '+inf'):
                    errors.append('节电比例不能为 NaN/Infinity')
                    row_errors.append({'row': int(idx) + 2, 'field': '节电比例', 'error': '非有限数'})
                else:
                    try:
                        r = float(row['节电比例'])
                        if not __import__('math').isfinite(r):
                            errors.append('节电比例不能为 NaN/Infinity')
                        elif not (0 <= r <= 1):
                            errors.append('节电比例须在 0–1')
                        else:
                            has_frac = True
                    except Exception:
                        errors.append('节电比例类型无效')
            if has_drop and has_frac:
                method = None
                if '计算方法' in ndf.columns and not _is_blank(row.get('计算方法')):
                    method = str(row.get('计算方法')).strip()
                if method not in ('power_drop_hours', 'baseline_fraction', '功率差', '比例'):
                    errors.append(
                        f'措施「{row.get("项目名称")}」同时填写功率降幅与节电比例，'
                        f'须指定计算方法（power_drop_hours 或 baseline_fraction）'
                    )
                    row_errors.append({'row': int(idx) + 2, 'field': '计算方法', 'error': '双模型冲突'})
            if '投资_元' in ndf.columns and not _is_blank(row.get('投资_元')):
                try:
                    v = float(row['投资_元'])
                    if not __import__('math').isfinite(v) or v < 0:
                        errors.append('投资不得为负或非有限数')
                except Exception:
                    errors.append('投资类型无效')

    return {
        'ok': len(errors) == 0,
        'errors': errors,
        'warnings': warnings,
        'colmap': colmap,
        'normalized': ndf,
        'row_errors': row_errors,
    }


def apply_import(content: bytes, filename: str, kind: str, mappings: list[FieldMapping], *, energy_price_policy: str = 'keep_manual') -> dict:
    """先无副作用校验+实例化，全部通过后再原子写入；失败不改 case_id / 不留空项目。"""
    df = read_table(content, filename)
    result = validate_mapped(df, kind, mappings)
    if not result['ok']:
        return {
            'accepted': False,
            'errors': result['errors'],
            'warnings': result.get('warnings', []),
            'row_errors': result.get('row_errors', []),
            'message': '校验失败，文件未被接受。原项目保持不变。',
        }

    ndf: pd.DataFrame = result['normalized']
    current = get_case()
    forking_from_demo = current.case_id == JINHUI_ID or current.protected or current.is_demo
    warnings = list(result.get('warnings') or [])

    # —— 内存草稿：不写库 ——
    if forking_from_demo:
        label = {
            'energy': f'上传企业·{filename}',
            'device': f'上传设备·{filename}',
            'retrofit': f'上传技改·{filename}',
        }.get(kind, f'上传·{filename}')
        base = CaseState(
            case_id='draft-pending',
            name=label,
            is_demo=False,
            protected=False,
            data_grade='annual',
            energy_years=[],
            devices=[],
            retrofits=[],
            scenario=ScenarioParams(
                power_drop_kw=None,
                hours=None,
                baseline_power_kw=None,
                pump_baseline_kwh=None,
                energy_price=None,
                emission_factor=0.4471,
                investment_cny=None,
                annual_maintenance_cny=None,
                use_demo_baseline=False,
                use_jinhui_reference=False,
                use_jinhui_tariff_reference=False,
                selected_measure_id=None,
                emission_factor_user_override=False,
                energy_price_user_override=False,
            ),
            sources=[],
            version_label='upload',
            access_token=None,
        )
    else:
        base = current.model_copy(deep=True)

    try:
        if kind == 'energy':
            years = []
            for _, row in ndf.iterrows():
                years.append(EnergyYear(
                    year=_strict_int_year(row['年份'], '年份'),
                    elec_10k_kwh=float(row['工业用电量_万kWh']),
                    cost_10k_cny=_opt_float(row, '电力费用_万元', ndf),
                    diesel_10k_l=_opt_float(row, '柴油使用量_万升', ndf),
                    diesel_cost_10k_cny=_opt_float(row, '柴油费用_万元', ndf),
                    note=(
                        str(row.get('边界说明'))
                        if '边界说明' in ndf.columns and not _is_blank(row.get('边界说明'))
                        else '用户提供·未核验'
                    ),
                    url=(
                        sanitize_user_url(row.get('来源URL', ''))
                        if '来源URL' in ndf.columns and not _is_blank(row.get('来源URL'))
                        else ''
                    ),
                    data_type='用户提供·未核验',
                ))
            if '来源URL' in ndf.columns:
                for _, row in ndf.iterrows():
                    if not _is_blank(row.get('来源URL')) and not sanitize_user_url(row.get('来源URL')):
                        warnings.append(
                            f'{_strict_int_year(row["年份"], "年份")}: 来源URL不是安全的 http/https 链接，已忽略'
                        )
            base.energy_years = years
            base.name = f'上传企业·{filename}'
            base.data_grade = 'annual'
            price = None
            for y in sorted(years, key=lambda x: x.year, reverse=True):
                if y.cost_10k_cny is not None and y.elec_10k_kwh:
                    price = y.cost_10k_cny / y.elec_10k_kwh
                    break
            sc = base.scenario
            sc.use_jinhui_reference = False
            policy = (energy_price_policy or 'keep_manual').strip() or 'keep_manual'
            had_manual = bool(getattr(sc, 'energy_price_user_override', False)) and sc.energy_price is not None
            old_price = sc.energy_price
            if had_manual and policy != 'use_imported':
                # 默认保留用户手动价；记录冲突但不覆盖
                if price is None:
                    warnings.append(
                        f'已保留手动电价 {old_price} 元/kWh（来源：用户手动覆盖）；导入表无电费列，未改写电价，energy_price_user_override 仍为 true'
                    )
                elif abs(float(price) - float(old_price)) > 1e-9:
                    warnings.append(
                        f'电价冲突：手动电价 {old_price} 元/kWh（用户手动覆盖）与导入推算价 {price} 元/kWh（能源表电力费用/用电量）不一致；已按默认策略保留手动价。若要改用导入价，请在提交时选择 energy_price_policy=use_imported'
                    )
                else:
                    warnings.append(f'导入推算电价与手动电价均为 {old_price} 元/kWh，已保留手动覆盖标记')
            else:
                if had_manual and policy == 'use_imported':
                    warnings.append(
                        f'已按用户选择改用导入电价：原手动 {old_price} 元/kWh → 新值 {price}（来源：能源表推算；无电费列则为空）'
                    )
                sc.energy_price = price
                sc.energy_price_user_override = False
            if forking_from_demo:
                sc.power_drop_kw = None
                sc.hours = None
                sc.investment_cny = None
                sc.selected_measure_id = None
                sc.pump_baseline_kwh = None
                base.devices = []
                base.retrofits = []
            base.scenario = sc
            warnings.append('年度能源数据标记为「用户提供·未核验」，不是公开披露。')

        elif kind == 'device':
            devices = []
            skipped_hours = []
            old_devices = list(base.devices or [])
            old_by_id = {d.id: d for d in old_devices if d.id}
            unused_ids = set(old_by_id)
            removed_ids = {d.id for d in (getattr(base, 'removed_devices', None) or []) if d.id}
            name_index: dict[str, list[str]] = {}
            for d in old_devices:
                name_index.setdefault(d.name, []).append(d.id)
            taken_ids: set[str] = set()
            for _, row in ndf.iterrows():
                hours = _opt_float(row, '年运行小时', ndf)
                if hours is None:
                    skipped_hours.append(str(row['设备名称']))
                # V8 P0-06：用户上传不得自行升为「披露/实测」认证身份
                dtype_raw = str(row['数据类型']) if '数据类型' in ndf.columns and not _is_blank(row.get('数据类型')) else ''
                claimed_source = dtype_raw  # 仅作备注/自称来源，不升格核验状态
                data_type = '用户提供·未核验'
                name = str(row['设备名称']).strip()
                device_type = str(row['设备类型']) if '设备类型' in ndf.columns and not _is_blank(row.get('设备类型')) else '通用'
                power_kw = float(row['功率_kW'])
                reused = None
                explicit_id = None
                if '设备ID' in ndf.columns and not _is_blank(row.get('设备ID')):
                    claimed = str(row.get('设备ID')).strip()
                    explicit_id = claimed
                    if claimed in unused_ids and claimed not in taken_ids:
                        reused = claimed
                    elif claimed in taken_ids:
                        raise ValueError(
                            f'设备「{name}」的设备ID「{claimed}」在本次导入中重复，请使用唯一编号'
                        )
                    else:
                        # 显式新 ID：不得再按同名回退复用旧设备（避免旧技改静默继承）
                        reused = None
                if reused is None and explicit_id is None:
                    cands = [i for i in name_index.get(name, []) if i in unused_ids]
                    if len(cands) == 1:
                        reused = cands[0]
                if reused is None and explicit_id is None:
                    sig_cands = []
                    for oid in list(unused_ids):
                        od = old_by_id[oid]
                        if (
                            od.name == name
                            and (od.device_type or '通用') == device_type
                            and od.power_kw == power_kw
                            and od.hours == hours
                        ):
                            sig_cands.append(oid)
                    if len(sig_cands) == 1:
                        reused = sig_cands[0]
                if reused:
                    unused_ids.discard(reused)
                    did = reused
                    taken_ids.add(did)
                elif explicit_id:
                    # 作为新设备，优先采用用户给出的显式 ID；先避开回收站，再避开活动区与本批已用 ID
                    if explicit_id in removed_ids:
                        did = _unique_prefixed_id('dev', set(taken_ids) | unused_ids | removed_ids); taken_ids.add(did)
                        warnings.append(
                            f'设备「{name}」的设备ID「{explicit_id}」与回收站中已删除设备冲突，已分配新ID「{did}」，回收站原设备仍可恢复'
                        )
                    elif explicit_id not in taken_ids and explicit_id not in unused_ids:
                        did = explicit_id
                        taken_ids.add(did)
                        warnings.append(
                            f'设备「{name}」使用显式新设备ID「{did}」，不继承同名旧设备的技改绑定'
                        )
                    else:
                        did = _unique_prefixed_id('dev', set(taken_ids) | unused_ids | removed_ids); taken_ids.add(did)
                        warnings.append(
                            f'设备「{name}」使用显式新设备ID「{did}」，不继承同名旧设备的技改绑定'
                        )
                else:
                    blocked = taken_ids | unused_ids | removed_ids
                    did = _unique_prefixed_id('dev', blocked)
                    taken_ids.add(did)
                devices.append(Device(
                    id=did,
                    name=name,
                    device_type=device_type,
                    power_kw=power_kw,
                    hours=hours,
                    efficiency=_opt_float(row, '效率', ndf),
                    baseline_kwh=_opt_float(row, '基准年电耗_kWh', ndf),
                    output_t=_opt_float(row, '产量_t', ndf),
                    data_type=data_type,  # type: ignore
                    source=( (str(row['数据来源']) if '数据来源' in ndf.columns and not _is_blank(row.get('数据来源')) else '用户提供·未核验') + (f'｜自称:{claimed_source}' if claimed_source else '')),
                ))
            base.devices = devices
            base.data_grade = 'device'
            if forking_from_demo or base.is_demo or base.protected:
                base.name = f'上传设备·{filename}'
                base.retrofits = []
                sc = base.scenario
                sc.use_jinhui_reference = False
                sc.power_drop_kw = None
                sc.hours = None
                sc.investment_cny = None
                sc.selected_measure_id = None
                sc.pump_baseline_kwh = None
                sc.investment_bound_measure_id = None
                sc.pump_baseline_bound_measure_id = None
                sc.pump_baseline_bound_device_id = None
                sc.maintenance_bound_measure_id = None
                base.scenario = sc
            else:
                valid_ids = {d.id for d in devices}
                kept = []
                detached = []
                for r in list(base.retrofits or []):
                    if r.device_id and r.device_id in valid_ids:
                        kept.append(r)
                    else:
                        detached.append(r)
                base.retrofits = kept
                if detached:
                    if not getattr(base, 'removed_retrofits', None):
                        base.removed_retrofits = []
                    existing_rm = {rf.id for rf in base.removed_retrofits}
                    for rf in detached:
                        if rf.id in existing_rm:
                            rf.id = _unique_prefixed_id('rf', existing_rm)
                        else:
                            existing_rm.add(rf.id)
                        base.removed_retrofits.append(rf)
                    warnings.append(
                        f'有 {len(detached)} 条技改无法自动绑定到新设备清单，已移入回收站，可恢复，不会无声删除'
                    )
                if base.scenario.selected_measure_id and not any(
                    r.id == base.scenario.selected_measure_id for r in kept
                ):
                    base.scenario.selected_measure_id = None
                    base.scenario.power_drop_kw = None
                    base.scenario.investment_cny = None
            if skipped_hours:
                warnings.append(
                    f'以下设备未提供运行小时（保持空，不计入汇总）: {", ".join(skipped_hours)}'
                )
            warnings.append('导入设备数据标记为「用户提供·未核验」，不得视为企业公开披露。')
            # 与手工改类型一致：复用旧 ID 但类型变化时，失效不适用的专业参考
            try:
                from .change_validation import on_device_type_changed
                for d in devices:
                    old_d = old_by_id.get(d.id)
                    if old_d is not None and (old_d.device_type or '') != (d.device_type or ''):
                        on_device_type_changed(base, d)
            except Exception:
                pass
            _invalidate_orphan_bindings(base)

        elif kind == 'retrofit':
            # P0-13：同名设备禁止仅用名称自动绑定
            name_counts: dict[str, int] = {}
            for d in base.devices:
                name_counts[d.name] = name_counts.get(d.name, 0) + 1
            name_to_ids: dict[str, list[str]] = {}
            for d in base.devices:
                name_to_ids.setdefault(d.name, []).append(d.id)
            id_set = {d.id for d in base.devices}

            items = []
            bind_errors = []
            incomplete = []
            taken_rf_ids = {r.id for r in (base.retrofits or []) if r.id}
            taken_rf_ids.update(r.id for r in (getattr(base, 'removed_retrofits', None) or []) if r.id)
            for _, row in ndf.iterrows():
                claimed_source = str(row['数据类型']) if '数据类型' in ndf.columns and not _is_blank(row.get('数据类型')) else ''
                target_name = str(row['目标设备'])
                device_id = None
                if '设备ID' in ndf.columns and not _is_blank(row.get('设备ID')):
                    device_id = str(row['设备ID']).strip()
                    if base.devices and device_id not in id_set:
                        bind_errors.append(
                            f'措施「{row["项目名称"]}」的设备ID「{device_id}」不在当前设备清单'
                        )
                        continue
                else:
                    candidates = name_to_ids.get(target_name) or []
                    if len(candidates) > 1:
                        bind_errors.append(
                            f'措施「{row["项目名称"]}」目标设备「{target_name}」存在同名多台，'
                            f'请在表格填写唯一设备ID（候选: {", ".join(candidates)}）'
                        )
                        continue
                    if len(candidates) == 1:
                        device_id = candidates[0]
                    else:
                        # 模糊匹配也禁止在多候选时自动挑选
                        fuzzy = [
                            (n, ids) for n, ids in name_to_ids.items()
                            if target_name in n or n in target_name
                        ]
                        flat = [i for _, ids in fuzzy for i in ids]
                        if len(flat) > 1:
                            bind_errors.append(
                                f'措施「{row["项目名称"]}」目标「{target_name}」匹配到多台设备，'
                                f'请填写唯一设备ID'
                            )
                            continue
                        if len(flat) == 1:
                            device_id = flat[0]
                if device_id is None and base.devices:
                    bind_errors.append(f'措施「{row["项目名称"]}」无法绑定目标设备「{target_name}」')
                    continue

                drop = _opt_float(row, '功率降低_kW', ndf)
                frac = _opt_float(row, '节电比例', ndf)
                method = None
                if '计算方法' in ndf.columns and not _is_blank(row.get('计算方法')):
                    raw_m = str(row.get('计算方法')).strip()
                    if raw_m in ('power_drop_hours', '功率差'):
                        method = 'power_drop_hours'
                    elif raw_m in ('baseline_fraction', '比例'):
                        method = 'baseline_fraction'
                if drop is not None and frac is not None and method is None:
                    bind_errors.append(
                        f'措施「{row["项目名称"]}」同时填写功率降幅与节电比例，须指定计算方法'
                    )
                    continue
                if method == 'power_drop_hours':
                    frac = None
                elif method == 'baseline_fraction':
                    drop = None

                # P0-05：功率降幅 vs 设备功率
                if drop is not None and device_id:
                    dev = next((d for d in base.devices if d.id == device_id), None)
                    avg_run = None
                    if dev is not None:
                        # 改造前平均运行功率：优先 baseline_power 语义用 power_kw
                        avg_run = dev.power_kw
                    if avg_run is not None and drop > avg_run:
                        bind_errors.append(
                            f'措施「{row["项目名称"]}」功率降幅 {drop} kW 大于设备功率 {avg_run} kW，'
                            f'物理上不成立；请区分额定功率与改造前平均运行功率后重填'
                        )
                        continue

                rf = RetrofitProject(
                    id=_unique_prefixed_id('rf', taken_rf_ids),
                    name=str(row['项目名称']).strip(),
                    target_device=target_name,
                    device_id=device_id,
                    power_drop_kw=drop,
                    saving_fraction=frac,
                    calculation_method=method,
                    investment_cny=_opt_float(row, '投资_元', ndf),
                    annual_maintenance_cny=_opt_float(row, '年维护费_元', ndf),
                    source=( (str(row['数据来源']) if '数据来源' in ndf.columns and not _is_blank(row.get('数据来源')) else '用户提供·未核验') + (f'｜自称:{claimed_source}' if claimed_source else '')),
                    note=str(row['备注']) if '备注' in ndf.columns and not _is_blank(row.get('备注')) else '',
                    data_type='假设',
                )
                if rf.power_drop_kw is None and rf.saving_fraction is None:
                    incomplete.append(rf.name)
                items.append(rf)

            if bind_errors:
                return {
                    'accepted': False,
                    'errors': bind_errors,
                    'warnings': warnings,
                    'message': '技改校验失败，文件未接受。原项目保持不变。',
                }
            if not base.devices:
                warnings.append('当前无设备清单，技改已导入但 device_id 为空；请先上传/新增设备后重新绑定')
            if incomplete:
                warnings.append(
                    f'以下措施缺少功率降幅或节电比例，暂不可计算：{", ".join(incomplete)}'
                )

            base.retrofits = items
            if base.is_demo or base.protected:
                base.name = f'上传技改·{filename}'

            # P0-01：替换措施后清空失效选中与过期情景绑定
            sc = base.scenario
            sc.selected_measure_id = None
            sc.power_drop_kw = None
            sc.investment_cny = None
            sc.investment_bound_measure_id = None
            sc.maintenance_bound_measure_id = None
            # 替换措施后不得沿用示范/旧项目的「暂按0元试算」标记
            sc.assume_zero_maintenance = False
            sc.pump_baseline_bound_measure_id = None
            sc.pump_baseline_bound_device_id = None
            # 仅一项且可计算时可自动选中；多项请用户选
            computable = [
                it for it in items
                if it.power_drop_kw is not None or it.saving_fraction is not None
            ]
            if len(computable) == 1:
                it = computable[0]
                sc.selected_measure_id = it.id
                if it.power_drop_kw is not None:
                    sc.power_drop_kw = it.power_drop_kw
                if it.investment_cny is not None:
                    sc.investment_cny = it.investment_cny
                    sc.investment_bound_measure_id = it.id
                sc.annual_maintenance_cny = it.annual_maintenance_cny
                sc.maintenance_bound_measure_id = it.id
            elif len(computable) > 1:
                warnings.append('已导入多项技改措施，请在页面选择当前要评估的一项；不会沿用旧措施编号。')
            else:
                warnings.append('已替换技改列表，但尚无足够参数完成计算；请补齐后选择措施。')
            base.scenario = sc
            # 切换措施后解除可能错配的报价
            base.quote_choice = None
            _invalidate_orphan_bindings(base)

    except Exception as e:
        # Pydantic / 其它实例化错误：整次回滚，不写库
        return {
            'accepted': False,
            'errors': [str(e)],
            'warnings': warnings,
            'row_errors': result.get('row_errors', []),
            'message': '校验失败，文件未被接受。原项目保持不变。',
        }

    # —— 全部通过后一次原子写入 ——
    state = replace_active_working_copy(base)
    return {
        'accepted': True,
        'warnings': warnings,
        'case_id': state.case_id,
        'name': state.name,
        'data_grade': state.data_grade,
        'access_token': state.access_token,
        'message': '已导入并触发重新核算（金徽示范未被覆盖；新企业不继承金徽默认功率/投资）',
        'coverage_note': (
            '设备汇总仅含同时提供功率与小时的设备；缺小时设备已跳过'
            if kind == 'device' else None
        ),
    }
