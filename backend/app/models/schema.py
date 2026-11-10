from __future__ import annotations
import math
import unicodedata
from typing import Any, Literal, Optional
from urllib.parse import urlparse
from pydantic import BaseModel, Field, field_validator, model_validator


DataType = Literal['实测', '披露', '文献', '模型估算', '假设', '用户提供·未核验', '企业提供·未核验']
DataGrade = Literal['annual', 'device', 'hourly']
ValueStatus = Literal['known', 'null', 'estimated', 'assumed', 'reported']


# 业务合理上限：拦截 1e308 等“有限但会污染后续计算”的值
FIELD_ABS_LIMITS = {
    'power_kw': 1e7,
    'baseline_power_kw': 1e7,
    'power_drop_kw': 1e7,
    'baseline_kwh': 1e15,
    'pump_baseline_kwh': 1e15,
    'output_t': 1e12,
    'energy_price': 1e5,
    'emission_factor': 100.0,
    'investment_cny': 1e15,
    'annual_maintenance_cny': 1e15,
    'elec_10k_kwh': 1e10,
    'cost_10k_cny': 1e12,
    'diesel_10k_l': 1e10,
    'diesel_cost_10k_cny': 1e12,
    'value': 1e15,
    'total_quote_cny': 1e15,
    'budget_cny': 1e15,
    'sensitivity_investment_cny': 1e15,
}


def _finite(v: Any, field: str) -> Any:
    if v is None:
        return v
    if isinstance(v, bool):
        raise ValueError(f'{field} 必须是数值')
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f'{field} 无法解析为数值')
    if not math.isfinite(f):
        raise ValueError(f'{field} 不能为 NaN/Infinity')
    hi = FIELD_ABS_LIMITS.get(field)
    if hi is not None and abs(f) > hi:
        raise ValueError(f'{field} 超出合理范围（上限 {hi:g}）')
    return f


def _finite_nonneg(v: Any, field: str) -> Any:
    """有限且非负；None 保持缺失（缺失≠0）。"""
    f = _finite(v, field)
    if f is not None and f < 0:
        raise ValueError(f'{field} 不得为负')
    return f


def _required_name(v: Any, field: str = 'name') -> str:
    if v is None:
        raise ValueError(f'{field} 不能为空')
    s = str(v).strip()
    if not s or s.lower() in ('nan', 'none', 'null'):
        raise ValueError(f'{field} 不能为空')
    return s


def _strict_int_year(v: Any, field: str = 'year') -> int:
    if v is None or (isinstance(v, str) and not str(v).strip()):
        raise ValueError(f'{field} 不能为空')
    if isinstance(v, bool):
        raise ValueError(f'{field} 必须是整数')
    if isinstance(v, int):
        y = v
    else:
        raw = unicodedata.normalize('NFKC', str(v).strip())
        try:
            f = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f'{field} 无法解析为整数')
        if not math.isfinite(f) or f != int(f):
            raise ValueError(f'{field} 必须是整数，不能带小数')
        y = int(f)
    if y < 1990 or y > 2100:
        raise ValueError(f'{field} 超出合理范围 1990–2100：{y}')
    return y


def sanitize_user_url(v: Any) -> str:
    """仅保留 http/https 外链；javascript/data/file 等一律置空。"""
    if v is None:
        return ''
    text = str(v).strip()
    if not text:
        return ''
    try:
        parsed = urlparse(text)
    except Exception:
        return ''
    if parsed.scheme.lower() not in ('http', 'https'):
        return ''
    if not parsed.netloc:
        return ''
    return text


class ParamSource(BaseModel):
    value: Any = None
    unit: str = ''
    data_type: DataType = '假设'
    source_name: str = ''
    source_year: Optional[int] = None
    source_url: str = ''
    scope: str = ''
    status: ValueStatus = 'assumed'

    @field_validator('source_url', mode='before')
    @classmethod
    def safe_param_url(cls, v):
        return sanitize_user_url(v)


class EnergyYear(BaseModel):
    year: int
    elec_10k_kwh: float
    # 费用可为 None：缺失≠0
    cost_10k_cny: Optional[float] = None
    diesel_10k_l: Optional[float] = None
    diesel_cost_10k_cny: Optional[float] = None
    note: str = ''
    url: str = ''
    data_type: DataType = '披露'
    # V8 P0-07 边界：同 boundary_id/site_id 才可与设备节电硬比
    boundary_id: Optional[str] = None
    site_id: Optional[str] = None
    measurement_scope: Optional[str] = 'enterprise_total'
    calendar_year: Optional[int] = None
    energy_type: Optional[str] = 'electricity'

    @field_validator('year', mode='before')
    @classmethod
    def year_range(cls, v, info):
        return _strict_int_year(v, 'year')

    @field_validator('url', mode='before')
    @classmethod
    def safe_energy_url(cls, v):
        return sanitize_user_url(v)

    @field_validator('elec_10k_kwh', 'cost_10k_cny', 'diesel_10k_l', 'diesel_cost_10k_cny', mode='before')
    @classmethod
    def finite_energy(cls, v, info):
        return _finite_nonneg(v, info.field_name)


class Device(BaseModel):
    id: str
    name: str
    device_type: str = '通用'
    # None = 未提供；0 = 真实零。禁止把缺失写成 0。
    power_kw: Optional[float] = None
    hours: Optional[float] = None
    efficiency: Optional[float] = None
    baseline_kwh: Optional[float] = None
    output_t: Optional[float] = None
    data_type: DataType = '假设'
    source: str = '用户录入'
    # V8 P0-07 边界字段
    boundary_id: Optional[str] = None
    site_id: Optional[str] = None
    measurement_scope: Optional[str] = 'device'
    accounting_year: Optional[int] = None
    # 软删除进入回收站时写入 ISO 时间；恢复后清空。不参与计算。
    removed_at: Optional[str] = None

    @field_validator('name', mode='before')
    @classmethod
    def name_required(cls, v):
        return _required_name(v, '设备名称')

    @field_validator('power_kw', 'hours', 'efficiency', 'baseline_kwh', 'output_t', mode='before')
    @classmethod
    def finite_device(cls, v, info):
        f = _finite_nonneg(v, info.field_name)
        if info.field_name == 'hours' and f is not None and f > 8784:
            raise ValueError('hours 不可超过 8784')
        if info.field_name == 'efficiency' and f is not None and not (0 <= f <= 1):
            raise ValueError('efficiency 须在 0—1')
        return f


METHOD_CODE_LITERAL = Literal[
    'power_drop_hours',
    'baseline_fraction',
    'pump_hydraulic',
    'fan_pressure_flow',
    'motor_efficiency',
    'compressor_power_drop',
    'transformer_losses',
]
PROFESSIONAL_METHOD_CODES = (
    'pump_hydraulic',
    'fan_pressure_flow',
    'motor_efficiency',
    'compressor_power_drop',
    'transformer_losses',
)


class ReferenceSelection(BaseModel):
    """用户确认过的参考资料快照（写入案例，可追溯）。"""
    source_id: str
    verified_by_user: bool = True
    applicable_device_id: Optional[str] = None
    method_code: Optional[str] = None
    value_if_any: Optional[float] = None
    source_version: Optional[str] = None
    selected_at: Optional[str] = None
    choice: str = 'match_trial'  # match_trial | reject | use_own
    title: Optional[str] = None
    source_url: Optional[str] = None
    applied: bool = False
    parameter_key: Optional[str] = None
    parameter_value: Optional[float] = None
    unit: Optional[str] = None
    apply_intent: str = 'bookmark'  # bookmark | apply_to_trial | preview
    previous_value: Optional[float] = None
    measure_id: Optional[str] = None
    value_kind: Optional[str] = None  # 用户假设 | 企业输入 | 资料摘录
    binding_id: Optional[str] = None
    version: Optional[int] = None
    active: bool = False
    revoked: bool = False
    superseded: bool = False
    excerpt_locator: Optional[str] = None
    source_year: Optional[str] = None
    invalidated_reason: Optional[str] = None

    @field_validator('source_url', mode='before')
    @classmethod
    def safe_ref_url(cls, v):
        return sanitize_user_url(v) or None


class RetrofitProject(BaseModel):
    id: str  # measure_id
    name: str
    target_device: str = ''  # 显示名（可重复）
    device_id: Optional[str] = None  # 强制绑定目标设备
    power_drop_kw: Optional[float] = None
    saving_fraction: Optional[float] = None
    # 通用：power_drop_hours | baseline_fraction；专业：pump_hydraulic 等
    calculation_method: Optional[METHOD_CODE_LITERAL] = None
    # 稳定方法代码（与 calculation_method 同步；专业模型优先读此字段）
    method_code: Optional[METHOD_CODE_LITERAL] = None
    # 专业模型工况输入（带单位语义由后端模型校验）；通用方法可为空
    model_inputs: Optional[dict[str, Any]] = None
    investment_cny: Optional[float] = None
    annual_maintenance_cny: Optional[float] = None  # None=未知；0=已确认无新增或明确假设
    data_type: DataType = '披露'
    source: str = ''
    note: str = ''
    revision: int = 1
    previous_snapshot: Optional[dict[str, Any]] = None
    lifecycle_status: Optional[str] = None  # active | draft | invalid
    copied_from_id: Optional[str] = None
    provenance_label: Optional[str] = None

    @field_validator('name', mode='before')
    @classmethod
    def name_required(cls, v):
        return _required_name(v, '技改名称')

    @field_validator(
        'power_drop_kw', 'saving_fraction', 'investment_cny', 'annual_maintenance_cny',
        mode='before',
    )
    @classmethod
    def finite_rf(cls, v, info):
        return _finite_nonneg(v, info.field_name)

    @model_validator(mode='after')
    def check_fraction(self):
        # 同步 method_code <-> calculation_method
        mc = self.method_code or self.calculation_method
        if self.method_code is None and self.calculation_method is not None:
            self.method_code = self.calculation_method
        if self.calculation_method is None and self.method_code is not None:
            self.calculation_method = self.method_code
        if self.saving_fraction is not None and not (0 <= self.saving_fraction <= 1):
            raise ValueError('节电比例须在 0—1')
        if self.investment_cny is not None and self.investment_cny < 0:
            raise ValueError('投资不得为负')
        if self.power_drop_kw is not None and self.power_drop_kw < 0:
            raise ValueError('功率降低不得为负')
        if self.annual_maintenance_cny is not None and self.annual_maintenance_cny < 0:
            raise ValueError('年维护费不得为负')
        both = self.power_drop_kw is not None and self.saving_fraction is not None
        resolved = self.method_code or self.calculation_method
        if both and resolved not in ('power_drop_hours', 'baseline_fraction'):
            raise ValueError(
                '同时填写功率降幅与节电比例时，必须指定 calculation_method='
                'power_drop_hours 或 baseline_fraction'
            )
        if resolved in PROFESSIONAL_METHOD_CODES and not self.model_inputs:
            raise ValueError(f'专业方法 {resolved} 需要提供 model_inputs 工况数据')
        return self


class CustomParam(BaseModel):
    name: str
    unit: str
    source: str
    target: str
    value: float
    mapped_model_field: Optional[str] = None
    data_type: DataType = '假设'

    @field_validator('value', mode='before')
    @classmethod
    def finite_val(cls, v, info):
        return _finite(v, info.field_name)


class ScenarioParams(BaseModel):
    # 新企业可不填；金徽示范默认有值
    power_drop_kw: Optional[float] = 200
    hours: Optional[float] = 4000
    baseline_power_kw: Optional[float] = None
    pump_baseline_kwh: Optional[float] = None
    efficiency: Optional[float] = None
    energy_price: Optional[float] = Field(default=6191.20 / 13612.92)
    emission_factor: Optional[float] = 0.4471
    investment_cny: Optional[float] = 1_000_000
    annual_maintenance_cny: Optional[float] = 0
    output_t: Optional[float] = None
    use_demo_baseline: bool = False
    # 显式启用金徽参考情景（新企业缺设备时不得隐式套用）
    use_jinhui_reference: bool = False
    use_jinhui_tariff_reference: bool = False  # 用户显式勾选金徽参考电价
    # 当前选中的技改措施
    selected_measure_id: Optional[str] = None
    # 因子是否用户改过（影响报告文案）
    emission_factor_user_override: bool = False
    energy_price_user_override: bool = False
    # 情景投资显式绑定到哪条措施（防跨措施继承）
    investment_bound_measure_id: Optional[str] = None
    # 情景级水泵/比例基准须绑定到具体措施或设备，禁止全案共享默认
    pump_baseline_bound_measure_id: Optional[str] = None
    pump_baseline_bound_device_id: Optional[str] = None
    # 情景运维费显式绑定到哪条措施
    maintenance_bound_measure_id: Optional[str] = None
    # V8 G04/G39：用户明确选择「暂按0元试算」后才允许把未知运维当 0
    assume_zero_maintenance: bool = False

    @field_validator(
        'power_drop_kw', 'hours', 'baseline_power_kw', 'pump_baseline_kwh',
        'efficiency', 'energy_price', 'emission_factor', 'investment_cny',
        'annual_maintenance_cny', 'output_t',
        mode='before',
    )
    @classmethod
    def finite_sc(cls, v, info):
        f = _finite_nonneg(v, info.field_name)
        if info.field_name == 'hours' and f is not None and f > 8784:
            raise ValueError('hours 不可超过 8784')
        if info.field_name == 'efficiency' and f is not None and not (0 <= f <= 1):
            raise ValueError('efficiency 须在 0—1')
        return f


class DataPrepItem(BaseModel):
    key: str
    label: str
    status: Literal['ready', 'assumed', 'needed', 'optional_advanced']
    detail: str = ''
    module: str = 'general'



class QuoteChoice(BaseModel):
    """用户显式确认的参考报价情景（切换报价不改变节电/减排）。"""
    quote_id: str
    project_id: str | None = None
    bidder_full_name: str | None = None
    label: str | None = None
    project_title: str | None = None
    total_quote_cny: float | None = None
    quote_kind: str | None = None  # public_bid | jinhui_reference | user_quote
    quote_usage_mode: str = 'explicit_reference_scenario'
    compatibility_result: str | None = None
    watermark: str | None = '参考报价情景'
    source_url: str | None = None
    applied_to_investment: bool = False
    compatibility_verified_by_user: bool = False
    sensitivity_investment_cny: Optional[float] = None
    is_full_retrofit_investment: bool = False
    # P0-11：报价必须绑定到措施/设备；切换后不匹配则解除
    measure_id: Optional[str] = None
    device_id: Optional[str] = None
    scope_verified_at: Optional[str] = None
    active: bool = True
    superseded: bool = False
    invalidated_reason: Optional[str] = None
    invalidated_at: Optional[str] = None

    @field_validator('source_url', mode='before')
    @classmethod
    def safe_quote_url(cls, v):
        return sanitize_user_url(v) or None


class CaseState(BaseModel):
    case_id: str
    name: str
    is_demo: bool = False
    origin_case_id: Optional[str] = None  # fork 来源，如 jinhui-demo
    protected: bool = False
    data_grade: DataGrade = 'annual'
    energy_years: list[EnergyYear] = []
    devices: list[Device] = []
    retrofits: list[RetrofitProject] = []
    # 软删除回收区：删除设备时移入，可恢复；不参与计算
    removed_devices: list[Device] = []
    removed_retrofits: list[RetrofitProject] = []
    custom_params: list[CustomParam] = []
    scenario: ScenarioParams = Field(default_factory=ScenarioParams)
    missing_fields: list[str] = []
    data_prep: list[DataPrepItem] = []
    sources: list[dict[str, Any]] = []
    version_label: str = 'current'
    # 项目访问凭证（只在服务端校验；序列化可返回给所有者）
    access_token: Optional[str] = None
    owner_session: Optional[str] = None
    quote_choice: Optional[QuoteChoice] = None
    user_quotes: list[dict[str, Any]] = []  # 仅本项目可见的私有询价
    # 用户确认过的参考资料快照（P1-02）
    reference_selections: list[ReferenceSelection] = []
    parameter_values: list[dict[str, Any]] = []
    needs_recalc: bool = False  # 编辑后未重算
    revision: int = 0  # 持久乐观锁；提交过期草稿须 409


class FieldMapping(BaseModel):
    user_column: str
    standard_field: str


class UploadPreviewRequest(BaseModel):
    template_kind: Literal['energy', 'device', 'retrofit']
    mappings: list[FieldMapping] = []


class AddDeviceRequest(BaseModel):
    name: str
    device_type: str = '通用'
    power_kw: Optional[float] = None
    # 默认 None：缺失≠4000
    hours: Optional[float] = None
    efficiency: Optional[float] = None
    baseline_kwh: Optional[float] = None
    output_t: Optional[float] = None
    data_type: DataType = '假设'
    source: str = '页面新增'

    @field_validator('name', mode='before')
    @classmethod
    def name_required(cls, v):
        return _required_name(v, '设备名称')

    @field_validator('power_kw', 'hours', 'efficiency', 'baseline_kwh', 'output_t', mode='before')
    @classmethod
    def finite_add(cls, v, info):
        f = _finite_nonneg(v, info.field_name)
        if info.field_name == 'hours' and f is not None and f > 8784:
            raise ValueError('hours 不可超过 8784')
        if info.field_name == 'efficiency' and f is not None and not (0 <= f <= 1):
            raise ValueError('efficiency 须在 0—1')
        return f


class UpdateDeviceRequest(BaseModel):
    name: Optional[str] = None
    device_type: Optional[str] = None
    power_kw: Optional[float] = None
    hours: Optional[float] = None
    efficiency: Optional[float] = None
    baseline_kwh: Optional[float] = None
    output_t: Optional[float] = None
    data_type: Optional[DataType] = None
    source: Optional[str] = None
    clear_power_kw: Optional[bool] = None
    clear_hours: Optional[bool] = None
    clear_baseline_kwh: Optional[bool] = None
    clear_output_t: Optional[bool] = None
    accounting_year: Optional[int] = None

    @field_validator('name', mode='before')
    @classmethod
    def name_optional(cls, v):
        if v is None:
            return v
        return _required_name(v, '设备名称')

    @field_validator('power_kw', 'hours', 'efficiency', 'baseline_kwh', 'output_t', mode='before')
    @classmethod
    def finite_upd(cls, v, info):
        f = _finite_nonneg(v, info.field_name)
        if info.field_name == 'hours' and f is not None and f > 8784:
            raise ValueError('hours 不可超过 8784')
        if info.field_name == 'efficiency' and f is not None and not (0 <= f <= 1):
            raise ValueError('efficiency 须在 0—1')
        return f


class AddRetrofitRequest(BaseModel):
    name: str
    device_id: str
    target_device: Optional[str] = None
    power_drop_kw: Optional[float] = None
    saving_fraction: Optional[float] = None
    calculation_method: Optional[METHOD_CODE_LITERAL] = None
    method_code: Optional[METHOD_CODE_LITERAL] = None
    model_inputs: Optional[dict[str, Any]] = None
    investment_cny: Optional[float] = None
    annual_maintenance_cny: Optional[float] = None
    data_type: DataType = '假设'
    source: str = '页面新增'
    note: str = ''
    select: bool = True  # 新增后是否设为当前措施

    @field_validator('name', mode='before')
    @classmethod
    def name_required(cls, v):
        return _required_name(v, '技改名称')

    @field_validator('power_drop_kw', 'saving_fraction', 'investment_cny', 'annual_maintenance_cny', mode='before')
    @classmethod
    def finite_add_rf(cls, v, info):
        f = _finite_nonneg(v, info.field_name)
        if info.field_name == 'saving_fraction' and f is not None and not (0 <= f <= 1):
            raise ValueError('节电比例须在 0—1')
        return f

    @model_validator(mode='after')
    def dual_method_guard(self):
        resolved = self.method_code or self.calculation_method
        if self.method_code is None and self.calculation_method is not None:
            self.method_code = self.calculation_method
        if self.calculation_method is None and self.method_code is not None:
            self.calculation_method = self.method_code
        both = self.power_drop_kw is not None and self.saving_fraction is not None
        if both and resolved not in ('power_drop_hours', 'baseline_fraction'):
            raise ValueError(
                '同时填写功率降幅与节电比例时，必须指定 calculation_method='
                'power_drop_hours 或 baseline_fraction'
            )
        if resolved in PROFESSIONAL_METHOD_CODES and not self.model_inputs:
            raise ValueError(f'专业方法 {resolved} 需要提供 model_inputs 工况数据')
        return self


class CompareSchemesRequest(BaseModel):
    measure_a_id: str
    measure_b_id: str
    budget_cny: Optional[float] = None

    @field_validator('budget_cny', mode='before')
    @classmethod
    def finite_budget(cls, v, info):
        return _finite_nonneg(v, info.field_name)

    @model_validator(mode='after')
    def distinct_measures(self):
        if self.measure_a_id and self.measure_b_id and self.measure_a_id == self.measure_b_id:
            raise ValueError('请选两项不同的改造措施，不能自己和自己比')
        return self


class UpdateScenarioRequest(BaseModel):
    power_drop_kw: Optional[float] = None
    hours: Optional[float] = None
    baseline_power_kw: Optional[float] = None
    pump_baseline_kwh: Optional[float] = None
    efficiency: Optional[float] = None
    energy_price: Optional[float] = None
    emission_factor: Optional[float] = None
    investment_cny: Optional[float] = None
    annual_maintenance_cny: Optional[float] = None
    output_t: Optional[float] = None
    use_demo_baseline: Optional[bool] = None
    use_jinhui_reference: Optional[bool] = None
    use_jinhui_tariff_reference: Optional[bool] = None
    assume_zero_maintenance: Optional[bool] = None
    selected_measure_id: Optional[str] = None
    clear_baseline_power_kw: Optional[bool] = None
    clear_pump_baseline_kwh: Optional[bool] = None
    clear_investment_cny: Optional[bool] = None
    clear_power_drop_kw: Optional[bool] = None
    clear_hours: Optional[bool] = None
    clear_energy_price: Optional[bool] = None
    clear_annual_maintenance_cny: Optional[bool] = None

    @field_validator(
        'power_drop_kw', 'hours', 'baseline_power_kw', 'pump_baseline_kwh',
        'efficiency', 'energy_price', 'emission_factor', 'investment_cny',
        'annual_maintenance_cny', 'output_t',
        mode='before',
    )
    @classmethod
    def finite_upd_sc(cls, v, info):
        f = _finite_nonneg(v, info.field_name)
        if info.field_name == 'hours' and f is not None and f > 8784:
            raise ValueError('hours 不可超过 8784')
        return f


class CustomParamRequest(BaseModel):
    name: str
    unit: str
    source: str
    target: str
    value: float
    mapped_model_field: Optional[str] = None

    @field_validator('value', mode='before')
    @classmethod
    def finite_cp(cls, v, info):
        return _finite(v, info.field_name)


class ApplyQuoteRequest(BaseModel):
    quote_id: str
    compatibility_result: str = 'compatible'  # compatible | needs_adaptation | incompatible
    apply_to_investment: bool = False  # 禁止把采购候选价默认为完整技改投资
    as_price_sensitivity: bool = False  # 价格敏感性假设实验
    confirm_reference_scenario: bool = True
    confirm_spec_and_scope: bool = False  # 用户确认已核对型号/数量/范围
    measure_id: Optional[str] = None


class UserQuoteRequest(BaseModel):
    label: str
    total_quote_cny: float
    project_title: str = '用户自建询价'
    device_type: str = '变频器'
    note: str = ''
    source_url: str = ''

    @field_validator('source_url', mode='before')
    @classmethod
    def safe_user_quote_url(cls, v):
        return sanitize_user_url(v)

    @field_validator('total_quote_cny', mode='before')
    @classmethod
    def finite_uq(cls, v, info):
        f = _finite(v, info.field_name)
        if f is not None and f <= 0:
            raise ValueError('报价金额须大于 0')
        return f

    @model_validator(mode='after')
    def require_label(self):
        if not (self.label or '').strip():
            raise ValueError('报价须填写供应商/询价标签')
        return self


class ReportExportRequest(BaseModel):
    preset: str = 'full'  # full | one_page | custom
    sections: Optional[list[str]] = None  # None=按预设；[]=用户未勾选
    format: str = 'txt'  # txt|csv|html；pdf 提示用浏览器打印
