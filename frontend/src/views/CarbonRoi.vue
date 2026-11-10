<script setup lang="ts">
import { reactive, ref, watch, computed, onMounted } from 'vue'
import { state, applyPayload, refreshAll } from '../stores/app'
import { api } from '../api/client'
import MethodPanel from '../components/MethodPanel.vue'
import { safeHref } from '../api/safeHref'

const form = reactive({
  energy_price: null as number | null,
  emission_factor: 0.4471 as number | null,
  investment_cny: null as number | null,
  annual_maintenance_cny: null as number | null,
  assume_zero_maintenance: false,
  use_jinhui_tariff_reference: false,
})
const compareForm = reactive({
  measure_a_id: '',
  measure_b_id: '',
  budget_cny: null as number | null,
})
const compareResult = ref<any>(null)
const err = ref('')
const mill = computed(() => state.results?.selected_assessment || state.results?.mill_scenario)
const schemes = computed(() => state.results?.schemes || [])
const factorMeta = computed(() => state.results?.factor_meta || state.results?.defaults?.factor_meta)
const retrofits = computed(() => state.results?.case?.retrofits || [])
const catalog = ref<any>(null)
const selectedProject = ref('')
const selectedQuote = ref('')
const confirmCompat = ref(false)
const applyInvest = ref(false)
const quoteMsg = ref('')
const refSources = ref<any[]>([])
const refChoiceMsg = ref('')
const selectedRefId = ref('')
const refStep = ref(1)
const refPreview = ref<any>(null)
const userQuote = reactive({ label: '', total_quote_cny: null as number | null, device_type: '变频器' })

const categoryFilter = ref('') // '' | 变频器 | 水泵
const quotesOfProject = computed(() => {
  let all = (catalog.value?.quotes || []).filter((q: any) => q.quote_kind !== 'jinhui_reference')
  if (categoryFilter.value) {
    all = all.filter((q: any) => q.equipment_category === categoryFilter.value)
  }
  if (selectedProject.value) {
    all = all.filter((q: any) => q.project_id === selectedProject.value)
  }
  return all
})
const projectsForFilter = computed(() => {
  const all = catalog.value?.projects || []
  if (!categoryFilter.value) return all
  return all.filter((p: any) => p.equipment_category === categoryFilter.value)
})
function isActiveQuote(q: any) {
  if (!q) return false
  if (q.active === false || q.superseded) return false
  return true
}
const activeChoice = computed(() => {
  const snapQ = state.results?.assessment_snapshot?.quote_choice
  if (isActiveQuote(snapQ)) return snapQ
  const caseQ = state.results?.case?.quote_choice
  if (isActiveQuote(caseQ)) return caseQ
  const catQ = catalog.value?.active_quote_choice
  if (isActiveQuote(catQ)) return catQ
  return null
})
const historicalChoice = computed(() => {
  if (activeChoice.value) return null
  const hist = state.results?.assessment_snapshot?.historical_quote_choice
  if (hist) return hist
  const caseQ = state.results?.case?.quote_choice
  if (caseQ && !isActiveQuote(caseQ)) return caseQ
  return null
})
const selectedQuoteObj = computed(() => quotesOfProject.value.find((q: any) => q.quote_id === selectedQuote.value) || null)

function pickQuote(id: string) {
  selectedQuote.value = id
  const q = (catalog.value?.quotes || []).find((x: any) => x.quote_id === id)
  if (q?.project_id) selectedProject.value = q.project_id
}

function toggleQuote(id: string) {
  if (selectedQuote.value === id) {
    selectedQuote.value = ''
    return
  }
  pickQuote(id)
}

function clearQuote() {
  selectedQuote.value = ''
}

function money(n: number | null | undefined) {
  if (n == null || Number.isNaN(n)) return '金额未披露'
  return Number(n).toLocaleString('zh-CN') + ' 元'
}

watch(() => state.results?.case?.scenario, (sc) => {
  if (!sc) return
  form.energy_price = sc.energy_price
  form.emission_factor = sc.emission_factor
  form.investment_cny = sc.investment_cny
  form.annual_maintenance_cny = sc.annual_maintenance_cny ?? null
  form.assume_zero_maintenance = !!sc.assume_zero_maintenance
  form.use_jinhui_tariff_reference = !!sc.use_jinhui_tariff_reference
}, { immediate: true })

watch(retrofits, (list) => {
  if (!list?.length) return
  if (!compareForm.measure_a_id) compareForm.measure_a_id = list[0].id
  if (!compareForm.measure_b_id && list[1]) compareForm.measure_b_id = list[1].id
  else if (!compareForm.measure_b_id) compareForm.measure_b_id = list[0].id
}, { immediate: true })

function fmt(n: number | null | undefined, digits = 2) {
  if (n == null || Number.isNaN(n)) return null
  return Number(n).toLocaleString('zh-CN', { maximumFractionDigits: digits })
}

async function loadRefs() {
  try {
    const r = await api.references()
    refSources.value = r.sources || r.items || []
  } catch {
    refSources.value = []
  }
}

const selectedRef = computed(() => refSources.value.find((s: any) => (s.source_id || s.id) === selectedRefId.value) || null)
const saNow = computed(() => state.results?.selected_assessment || {})
const relatedRefFields = computed(() => {
  const method = saNow.value?.calculation_method || saNow.value?.source_trace?.calculation_method
  const all: Record<string, { key: string; label: string; unit: string }[]> = {
    pump_hydraulic: [
      { key: 'eta_pump_before', label: '改造前泵效率', unit: '0-1' },
      { key: 'eta_pump_after', label: '改造后泵效率', unit: '0-1' },
      { key: 'eta_motor', label: '电机效率', unit: '0-1' },
      { key: 'rho_kg_m3', label: '介质密度', unit: 'kg/m³' },
      { key: 'q_m3_h', label: '流量', unit: 'm³/h' },
      { key: 'head_m', label: '扬程', unit: 'm' },
      { key: 'hours', label: '年有效小时', unit: 'h' },
    ],
    fan_pressure_flow: [
      { key: 'eta_fan_before', label: '改造前风机效率', unit: '0-1' },
      { key: 'eta_fan_after', label: '改造后风机效率', unit: '0-1' },
      { key: 'eta_motor', label: '电机效率', unit: '0-1' },
      { key: 'q_m3_s', label: '风量', unit: 'm³/s' },
      { key: 'delta_p_pa', label: '风压差', unit: 'Pa' },
      { key: 'safety_min_q_m3_s', label: '安全最低风量', unit: 'm³/s' },
      { key: 'hours', label: '年有效小时', unit: 'h' },
    ],
    motor_efficiency: [
      { key: 'eta_old', label: '改造前效率', unit: '0-1' },
      { key: 'eta_new', label: '改造后效率', unit: '0-1' },
      { key: 'p_shaft_kw', label: '轴功率', unit: 'kW' },
      { key: 'hours', label: '年有效小时', unit: 'h' },
    ],
  }
  return all[method] || [{ key: 'rho_kg_m3', label: '介质密度', unit: 'kg/m³' }]
})

function requireMeasureId(): string | null {
  if (!saNow.value?.measure_id) return '请先选择要测算的改造方案'
  return null
}

function humanRefError(e: any): string {
  const raw = e?.message || String(e || '操作失败')
  if (/^\d{3}$/.test(String(raw).trim())) return '这次操作没有完成，请检查填写的设备和参数后再试。'
  return raw
}

function refApplyBody(extra: Record<string, unknown>) {
  const current = currentBinding.value
  return {
    source_id: selectedRefId.value || null,
    device_id: saNow.value?.device_id,
    measure_id: saNow.value?.measure_id,
    method_code: saNow.value?.calculation_method,
    parameter_key: refApply.parameter_key,
    parameter_value: refApply.parameter_value,
    unit: refApply.unit,
    binding_id: extra.choice === 'reject' ? (current?.binding_id || extra.binding_id) : extra.binding_id,
    ...extra,
  }
}

async function chooseRef(choice: string) {
  refChoiceMsg.value = ''
  refPreview.value = null
  try {
    const r = await api.selectReference({ source_id: selectedRefId.value || null, choice, apply_intent: 'bookmark' })
    if (r.results) applyPayload(r)
    refChoiceMsg.value = r.note || r.label || choice
    if (choice === 'reject') refChoiceMsg.value = r.note || '这份资料与你选择的设备或改造方式不一致，不能用于当前测算。'
    if (choice === 'use_own') refChoiceMsg.value = r.note || '下一步，请填写企业实测值。确认保存后，原先采用的参考参数会失效。点击此按钮本身不会改变当前计算结果。'
    if (choice === 'match_trial') refStep.value = 2
  } catch (e: any) {
    refChoiceMsg.value = humanRefError(e)
  }
}

const refApply = reactive({
  parameter_key: '',
  parameter_value: null as number | null,
  unit: '',
})

const currentBindings = computed(() => {
  const sels = state.results?.case?.reference_selections || []
  return sels.filter((s: any) => s.applied && s.measure_id === saNow.value?.measure_id)
})
const currentBinding = computed(() => {
  const key = refApply.parameter_key
  return currentBindings.value.find((s: any) => !key || s.parameter_key === key) || currentBindings.value[0] || null
})
const sourceHasExtractedValue = computed(() => {
  const params = selectedRef.value?.extractable_parameters || []
  return params.some((p: any) => relatedRefFields.value.some((f) => f.key === (p.key || p.parameter_key)))
})

watch(() => refApply.parameter_key, (key) => {
  const f = relatedRefFields.value.find((x) => x.key === key)
  if (f && !refApply.unit) refApply.unit = f.unit
})

async function previewRefToTrial() {
  refChoiceMsg.value = ''
  const miss = requireMeasureId()
  if (miss) { refChoiceMsg.value = miss; return }
  try {
    const r = await api.selectReference(refApplyBody({ choice: 'match_trial', apply_intent: 'preview' }))
    refPreview.value = r.preview || null
    refChoiceMsg.value = r.note || '请核对旧值、新值与结果变化后再确认。'
    refStep.value = 3
  } catch (e: any) {
    refChoiceMsg.value = humanRefError(e)
  }
}

async function applyRefToTrial() {
  refChoiceMsg.value = ''
  const miss = requireMeasureId()
  if (miss) { refChoiceMsg.value = miss; return }
  try {
    const r = await api.selectReference(refApplyBody({
      choice: 'match_trial',
      apply_intent: 'apply_to_trial',
      confirm_for_calc: true,
    }))
    if (r.results) applyPayload(r)
    else await refreshAll()
    refChoiceMsg.value = r.note || (r.applied
      ? '已将参考参数用于当前措施，结果已重新计算。'
      : '已保存资料来源。这不会改变本次计算结果。')
    refStep.value = 4
  } catch (e: any) {
    refChoiceMsg.value = humanRefError(e)
  }
}

async function revokeRef() {
  refChoiceMsg.value = ''
  try {
    const r = await api.selectReference(refApplyBody({ choice: 'reject', apply_intent: 'bookmark' }))
    if (r.results) applyPayload(r)
    else await refreshAll()
    refChoiceMsg.value = r.note || '这份资料已不再用于当前测算；你后来填写的数据会保留。'
    refPreview.value = null
  } catch (e: any) {
    refChoiceMsg.value = humanRefError(e)
  }
}

async function loadCatalog() {
  try {
    catalog.value = await api.procurementCatalog()
    const n = catalog.value?.quotes?.length || 0
    const checked = catalog.value?.last_checked_at || catalog.value?.version || ''
    if (!n) {
      quoteMsg.value = '当前后端未返回公开候选报价。请确认采购目录已加载；网络失败时将展示上次人工核验快照（若有）。' + (checked ? `（核验：${checked}）` : '')
    } else if (catalog.value.access_note) {
      quoteMsg.value = catalog.value.access_note
    } else {
      quoteMsg.value = `已加载 ${n} 家公开候选报价（后端统一核验快照${checked ? ' · ' + checked : ''}），勾选一家即可。历史候选总价不是本企业的当前采购价。`
    }
  } catch (e: any) {
    catalog.value = { quotes: [], projects: [], quote_count: 0, disclaimer: '报价目录接口不可用，未使用前端硬编码目录。' }
    quoteMsg.value = '报价目录接口失败（' + (e.message || String(e)) + '）。请重启一键启动.bat 后再试；不会使用页面内置第二套报价。'
  }
}

watch(categoryFilter, () => {
  selectedQuote.value = ''
  // 类别切换时若当前项目不属于该类，清空项目筛选
  if (selectedProject.value && !projectsForFilter.value.some((p: any) => p.project_id === selectedProject.value)) {
    selectedProject.value = ''
  }
})

watch(selectedProject, () => {
  if (selectedQuote.value && !quotesOfProject.value.some((q: any) => q.quote_id === selectedQuote.value)) {
    selectedQuote.value = ''
  }
})

onMounted(() => { loadCatalog(); loadRefs() })

async function recalculate() {
  err.value = ''
  try {
    const body: Record<string, any> = {
      use_jinhui_tariff_reference: form.use_jinhui_tariff_reference,
      assume_zero_maintenance: form.assume_zero_maintenance,
    }
    if (!form.assume_zero_maintenance) {
      if (form.annual_maintenance_cny == null || form.annual_maintenance_cny === ('' as any)) {
        body.clear_annual_maintenance_cny = true
      } else {
        body.annual_maintenance_cny = form.annual_maintenance_cny
      }
    }
    if (!form.use_jinhui_tariff_reference) {
      if (form.energy_price != null) body.energy_price = form.energy_price
      else body.clear_energy_price = true
    }
    if (form.emission_factor != null) body.emission_factor = form.emission_factor
    if (form.investment_cny == null || (form.investment_cny as any) === '') {
      body.clear_investment_cny = true
    } else {
      body.investment_cny = form.investment_cny
    }
    const r = await api.updateScenario(body as any)
    applyPayload(r)
    await refreshAll()
  } catch (e: any) {
    err.value = e.message
  }
}

function onNextAction(a: { label?: string; target?: string }) {
  const t = a?.target || ''
  if (t.startsWith('carbon:tariff')) {
    const el = document.getElementById('tariff-section') || document.querySelector('[data-section=tariff]')
    el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
}

async function runCompare() {
  err.value = ''
  compareResult.value = null
  try {
    compareResult.value = await api.compareSchemes({
      measure_a_id: compareForm.measure_a_id,
      measure_b_id: compareForm.measure_b_id,
      budget_cny: compareForm.budget_cny,
    })
  } catch (e: any) {
    err.value = e.message
  }
}

function paybackText(m: any) {
  if (!m) return '—'
  if (m.simple_payback_years != null) return `${m.simple_payback_years} 年`
  return m.payback_message || m.cost_note || '—'
}

function budgetText(v: any) {
  if (v === true || v === 'within') return '在预算内'
  if (v === false || v === 'over') return '超预算'
  if (v === 'unknown') return '无法判断是否符合预算'
  return '—'
}

async function applySelectedQuote() {
  quoteMsg.value = ''
  if (!selectedQuote.value) {
    quoteMsg.value = '请先选择一家报价'
    return
  }
  try {
    const r = await api.applyQuote({
      quote_id: selectedQuote.value,
      compatibility_result: confirmCompat.value ? 'compatible' : 'needs_adaptation',
      apply_to_investment: applyInvest.value && confirmCompat.value,
      confirm_reference_scenario: true,
    })
    applyPayload(r)
    await refreshAll()
    await loadCatalog()
    quoteMsg.value = r.watermark || '已按参考报价情景试算'
    if (r.energy_unchanged === false || r.carbon_unchanged === false) {
      quoteMsg.value += '（警告：节电/碳不应变化）'
    }
  } catch (e: any) {
    quoteMsg.value = e.message
  }
}

async function addMyQuote() {
  quoteMsg.value = ''
  if (!userQuote.label || userQuote.total_quote_cny == null) {
    quoteMsg.value = '请填写询价名称与金额'
    return
  }
  try {
    await api.addUserQuote({
      label: userQuote.label,
      total_quote_cny: userQuote.total_quote_cny,
      device_type: userQuote.device_type,
    })
    await loadCatalog()
    quoteMsg.value = '已添加我的询价（仅本项目可见）'
  } catch (e: any) {
    quoteMsg.value = e.message
  }
}
</script>

<template>
  <div class="notice">本页导航：①电费与维护费　②设备采购报价　③两种方案比较</div>

  <div class="eyebrow">投资与报价</div>
  <h1>电费、采购报价与回本比较</h1>
  <p class="lead">先填电费与维护费，再看设备采购报价，最后比较两种方案。更换报价不会改变节电和减排结果。</p>

  <div class="twocol">
    <div class="card">
      <span class="tag tag-abs">用户填写</span>
      <label class="field">
        <input type="checkbox" v-model="form.use_jinhui_tariff_reference" />
        采用金徽参考电价试算（{{ state.results?.defaults?.jinhui_tariff_reference?.toFixed?.(6) || '0.454803' }} 元/kWh）
      </label>
      <p class="muted">须显式勾选；勾选后标「参考价格、并非本企业实际电价」。</p>
      <label class="field">本企业电价 / 费用强度 元/kWh
        <input type="number" step="0.0001" v-model.number="form.energy_price" :disabled="form.use_jinhui_tariff_reference" />
      </label>
      <label class="field">排放因子 kgCO₂/kWh
        <input type="number" step="0.0001" v-model.number="form.emission_factor" />
      </label>
      <p class="muted">当前因子说明：{{ factorMeta?.label || factorMeta?.source_name || '—' }}</p>
      <label class="field">改造投资 元（可留空）
        <input type="number" v-model.number="form.investment_cny" placeholder="待输入投资" />
      </label>
      <label class="field">年新增运维费 元（可留空=未知）
        <input type="number" v-model.number="form.annual_maintenance_cny" placeholder="未知则留空" />
      </label>
      <label class="field">
        <input type="checkbox" v-model="form.assume_zero_maintenance" />
        暂按 0 元试算（假设无新增运维；结果会标明假设）
      </label>
      <p class="muted">改造后每年可能增加维护费。未填写且未勾选假设时，不给出确定回本与投资推荐。</p>
      <button class="btn" @click="recalculate">重新计算</button>
      <div v-if="err" class="error">{{ err }}</div>
    </div>

    <div class="card">
      <span class="tag tag-abs">情景估算</span>
      <h3>当前方案</h3>
      <div v-if="mill && (mill.status === 'ok' || mill.status === 'partial')">
        <div v-if="mill.recommendation_blocked" class="notice warn">{{ mill.recommendation_block_reason || mill.payback_message || '暂不能做确定投资推荐' }}</div>
        <div class="label">节电（不随电价/投资/报价变）</div>
        <div class="metric">{{ fmt(mill.saved_kwh, 0) }} kWh</div>
        <div class="label" style="margin-top:8px">预计减少的电力相关排放</div>
        <div class="metric">{{ fmt(mill.co2_reduction_t) }} tCO₂</div>
        <div class="label" style="margin-top:8px">年电费节省</div>
        <div class="metric" :class="{ pending: mill.annual_cost_saving_cny == null }">
          {{ mill.annual_cost_saving_cny != null ? fmt(mill.annual_cost_saving_cny) + ' 元' : (mill.cost_note || '补充电价后可估算') }}
        </div>
        <div class="muted" v-if="mill.tariff_is_reference">参考价格、并非本企业实际电价</div>
        <div class="label" style="margin-top:8px">年净节省</div>
        <div class="metric">{{ fmt(mill.annual_net_saving_cny) ?? '—' }} 元</div>
        <div class="label" style="margin-top:8px">简单回收期</div>
        <div class="metric" :class="{ pending: mill.simple_payback_years == null }">{{ paybackText(mill) }}</div>
      </div>
      <div v-else class="notice warn">{{ mill?.error || mill?.message || '暂无完整结果' }}</div>
    </div>
  </div>

  <div class="section" id="procurement-quotes">
    <h2>看看有哪些设备采购报价</h2>
    <p class="lead">先选设备类别，再勾选一家报价公司（可取消）。公司名是历史候选供应商，不是采购方。</p>
    <div class="card">
      <label class="field">想买什么设备？
        <select v-model="categoryFilter">
          <option value="">全部公开采购</option>
          <option value="变频器">变频器（3 家历史报价）</option>
          <option value="水泵">水泵（3 家历史报价）</option>
        </select>
      </label>
      <label class="field">可比采购项目
        <select v-model="selectedProject">
          <option value="">全部项目</option>
          <option v-for="p in projectsForFilter" :key="p.project_id" :value="p.project_id">
            {{ p.project_title }} · {{ p.buyer }}
          </option>
        </select>
      </label>

      <div class="label" style="margin-top:8px">报价公司（可勾选 / 再点一次取消）</div>
      <p class="muted">当前 {{ quotesOfProject.length }} 家。同一时间只选一家；再点已选项可取消。</p>
      <div v-if="quotesOfProject.length" class="quote-list">
        <div
          v-for="q in quotesOfProject"
          :key="q.quote_id"
          class="quote-row"
          :class="{ 'is-on': selectedQuote === q.quote_id }"
          @click="toggleQuote(q.quote_id)"
        >
          <input
            type="checkbox"
            :checked="selectedQuote === q.quote_id"
            @click.stop="toggleQuote(q.quote_id)"
          />
          <div class="quote-body">
            <div class="quote-name">{{ q.bidder_full_name || q.label }}</div>
            <div class="quote-price">{{ money(q.total_quote_cny) }}</div>
            <div class="quote-meta">
              {{ q.project_title || '' }} · {{ q.equipment_category || '' }}
              <span v-if="q.rank"> · 候选第{{ q.rank }}名</span>
              <template v-if="safeHref(q.source_url)">
                · <a :href="safeHref(q.source_url)" target="_blank" rel="noopener" @click.stop>来源公告</a>
              </template>
            </div>
          </div>
        </div>
      </div>
      <div v-else class="notice warn">还没有可勾选的报价。请把类别改回「全部公开采购」，或点「刷新目录」。</div>
      <div class="btn-row" v-if="selectedQuoteObj" style="margin-top:8px">
        <span class="muted">已勾选：{{ selectedQuoteObj.bidder_full_name }}</span>
        <button type="button" class="btn secondary" @click="clearQuote">取消勾选</button>
      </div>

      <label class="field"><input type="checkbox" v-model="confirmCompat" /> 我已核对型号、数量和采购范围，知道这是历史项目报价</label>
      <label class="field"><input type="checkbox" v-model="applyInvest" :disabled="!confirmCompat" /> 用这份报价看看预算变化（仅价格敏感性假设，不是完整技改投资）</label>
      <div class="btn-row">
        <button class="btn" @click="applySelectedQuote">用这份报价看看预算变化</button>
        <button class="btn secondary" @click="loadCatalog">刷新目录</button>
      </div>
      <p class="notice" v-if="activeChoice">
        当前采用：{{ activeChoice.bidder_full_name || activeChoice.label }}
        <span class="tag">{{ activeChoice.watermark || '参考报价情景' }}</span>
      </p>
      <p class="notice warn" v-else-if="historicalChoice">
        曾经引用·已失效：{{ historicalChoice.bidder_full_name || historicalChoice.label || '参考报价' }}
        （原因：{{ historicalChoice.invalidated_reason || '条件已变化' }}；时间：{{ historicalChoice.invalidated_at || '—' }}）
      </p>
      <p class="muted">{{ catalog?.disclaimer }}</p>
      <div v-if="quoteMsg" class="notice">{{ quoteMsg }}</div>
    </div>

    <div class="card" style="margin-top:12px">
      <h3>添加我拿到的供应商报价</h3>
      <label class="field">名称
        <input v-model="userQuote.label" placeholder="例如：本地经销商询价" />
      </label>
      <label class="field">金额 元
        <input type="number" v-model.number="userQuote.total_quote_cny" />
      </label>
      <label class="field">类别
        <select v-model="userQuote.device_type">
          <option>变频器</option>
          <option>水泵</option>
          <option>其他</option>
        </select>
      </label>
      <button class="btn secondary" @click="addMyQuote">保存为我的询价（仅本项目可见）</button>
    </div>
  </div>


  <div class="section">
    <h2>参考资料确认（不会自动进投资）</h2>
    <div class="card">
      <p class="muted">四步：查看来源 → 核对规格 → 预览差异 → 确认应用或撤销。收藏资料不会改变计算结果。</p>
      <p class="muted">当前步骤：{{ refStep }} / 4</p>
      <label class="field">1. 查看来源
        <select v-model="selectedRefId" @change="refStep = 1">
          <option value="">（可不选具体条目，只表态）</option>
          <option v-for="s in refSources" :key="s.source_id || s.id" :value="s.source_id || s.id">
            {{ s.title || s.name || s.source_id || s.id }}
          </option>
        </select>
      </label>
      <p class="muted" v-if="selectedRef">
        {{ selectedRef.note || '国家标准或指南链接是方法与合规依据，不是这台设备的实测效率。' }}
        <span v-if="safeHref(selectedRef.source_url)"> ｜ <a :href="safeHref(selectedRef.source_url)" target="_blank" rel="noopener">查看资料</a></span>
      </p>
      <p class="notice" v-if="selectedRef && !sourceHasExtractedValue">
        这份资料没有提供这台设备可直接套用的型号效率。请先查看资料；若自行填写数值，将作为试算假设，而不是资料里的实测值。
      </p>
      <p class="muted" v-if="currentBinding">
        当前使用来源：{{ currentBinding.title || currentBinding.source_id }}
        （{{ currentBinding.parameter_key || '参数' }} = {{ currentBinding.parameter_value ?? '—' }}，
        {{ currentBinding.value_kind || '用户假设' }}）
      </p>
      <div class="twocol" style="margin-top:8px">
        <label class="field">2. 核对规格（仅显示当前措施相关字段）
          <select v-model="refApply.parameter_key">
            <option value="">请选择参数</option>
            <option v-for="f in relatedRefFields" :key="f.key" :value="f.key">{{ f.label }}（{{ f.key }} / {{ f.unit }}）</option>
          </select>
        </label>
        <label class="field">数值<input type="number" step="any" v-model.number="refApply.parameter_value" /></label>
        <label class="field">单位<input v-model="refApply.unit" placeholder="如 0-1" /></label>
      </div>
      <p class="muted" v-if="saNow.device_name">将核对于：{{ saNow.device_name }} — {{ saNow.measure_name || '当前措施' }}（{{ saNow.calculation_method || '待选方法' }}）</p>
      <p class="warn" v-if="!saNow.measure_id">请先在改造页选择要测算的改造方案，再预览或确认用于试算。</p>
      <div v-if="refPreview" class="notice">
        旧值 {{ refPreview.old_value ?? '—' }} → 新值 {{ refPreview.new_value ?? '—' }}；
        节电 {{ refPreview.old_saved_kwh ?? '—' }} → {{ refPreview.new_saved_kwh ?? '—' }} kWh
        <span v-if="refPreview.value_kind"> ｜ 数值性质：{{ refPreview.value_kind }}</span>
        <span v-if="refPreview.assumption_note"><br />{{ refPreview.assumption_note }}</span>
      </div>
      <div class="btn-row">
        <button class="btn secondary" type="button" @click="chooseRef('match_trial')">规格基本一致，进入核对</button>
        <button class="btn secondary" type="button" @click="chooseRef('reject')">这份资料不匹配</button>
        <button class="btn secondary" type="button" @click="chooseRef('use_own')">下一步，请填写企业实测值</button>
        <button class="btn secondary" type="button" @click="previewRefToTrial">3. 预览差异</button>
        <button class="btn" type="button" @click="applyRefToTrial">4. 确认用于试算</button>
        <button class="btn secondary" type="button" @click="revokeRef">撤销已应用参数</button>
      </div>
      <p class="notice" v-if="refChoiceMsg">{{ refChoiceMsg }}</p>
    </div>
  </div>

  <div class="section">
    <h2>方案 A / B 比较（不同技改）</h2>
    <div class="card">
      <label class="field">方案 A
        <select v-model="compareForm.measure_a_id">
          <option v-for="r in retrofits" :key="'a'+r.id" :value="r.id">{{ r.name }}</option>
        </select>
      </label>
      <label class="field">方案 B
        <select v-model="compareForm.measure_b_id">
          <option v-for="r in retrofits" :key="'b'+r.id" :value="r.id">{{ r.name }}</option>
        </select>
      </label>
      <label class="field">预算约束 元（可选）
        <input type="number" v-model.number="compareForm.budget_cny" placeholder="不限" />
      </label>
      <button class="btn" @click="runCompare">对比方案</button>
    </div>
    <table class="data" v-if="compareResult">
      <thead>
        <tr><th></th><th>方案 A</th><th>方案 B</th></tr>
      </thead>
      <tbody>
        <tr><th>措施</th><td>{{ compareResult.A?.name }}</td><td>{{ compareResult.B?.name }}</td></tr>
        <tr><th>节电 kWh</th><td class="num">{{ fmt(compareResult.A?.saved_kwh, 0) ?? '—' }}</td><td class="num">{{ fmt(compareResult.B?.saved_kwh, 0) ?? '—' }}</td></tr>
        <tr><th>电力相关减排 tCO₂</th><td class="num">{{ fmt(compareResult.A?.co2_reduction_t) ?? '—' }}</td><td class="num">{{ fmt(compareResult.B?.co2_reduction_t) ?? '—' }}</td></tr>
        <tr><th>投资</th><td class="num">{{ compareResult.A?.investment_display ?? (compareResult.A?.investment_cny ?? '尚未填写') }}</td><td class="num">{{ compareResult.B?.investment_display ?? (compareResult.B?.investment_cny ?? '尚未填写') }}</td></tr>
        <tr><th>回收期</th><td>{{ paybackText(compareResult.A) }}</td><td>{{ paybackText(compareResult.B) }}</td></tr>
        <tr><th>预算</th><td>{{ budgetText(compareResult.A?.within_budget) }}</td><td>{{ budgetText(compareResult.B?.within_budget) }}</td></tr>
      </tbody>
    </table>
    <p class="notice" v-if="compareResult?.recommendation">说明：方案 {{ compareResult.recommendation.choice }} — {{ compareResult.recommendation.reason }}</p>
    <p class="warn" v-else-if="compareResult?.compare_only">{{ compareResult.compare_only_reason || compareResult.explanation || '目前只能比较节电与减碳，不能做经济回收期排名。' }}</p>
    <p class="muted" v-else-if="compareResult">投资未知时只比较、不推荐。</p>
    <div v-if="compareResult?.next_actions?.length" class="notice">
      <strong>下一步：</strong>
      <button
        v-for="(a, i) in compareResult.next_actions"
        :key="i"
        class="btn ghost"
        style="margin:0.25rem"
        type="button"
        @click="onNextAction(a)"
      >{{ a.label }}</button>
    </div>
  </div>

  <div class="section">
    <h2>同措施不同小时（2000 / 4000 / 6000 h）</h2>
    <table class="data">
      <thead>
        <tr>
          <th>小时</th><th>节电 kWh</th><th>电力相关减排 tCO₂</th><th>节费 元</th><th>回收期</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="s in schemes" :key="s.hours">
          <td>{{ s.hours }}</td>
          <td class="num">{{ s.error || fmt(s.annual_electricity_saved_kwh, 0) }}</td>
          <td class="num">{{ fmt(s.scenario_grid_co2_saved_t) ?? '—' }}</td>
          <td class="num">{{ fmt(s.scenario_electricity_cost_saved_cny) ?? '—' }}</td>
          <td class="num">{{ s.simple_payback_years != null ? s.simple_payback_years : (s.payback_message || '—') }}</td>
        </tr>
      </tbody>
    </table>
  </div>

  <MethodPanel :mill="mill" />
</template>

<style scoped>
.btn-row { display: flex; flex-wrap: wrap; gap: 8px; }
table.data { max-width: 100%; overflow-x: auto; }
@media (max-width: 390px) {
  table.data { font-size: 12px; display: block; overflow-x: auto; }
  .twocol { display: block; }
  .btn, button.btn { min-height: 36px; }
}
</style>
