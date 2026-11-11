<script setup lang="ts">
import { reactive, ref, watch, computed, onMounted } from 'vue'
import { state, applyPayload, refreshAll } from '../stores/app'
import { api } from '../api/client'
import MethodPanel from '../components/MethodPanel.vue'

const form = reactive({
  hours: 4000 as number | null,
  power_drop_kw: 200 as number | null,
  baseline_power_kw: null as number | null,
  pump_baseline_kwh: null as number | null,
  use_demo_baseline: false,
  use_jinhui_reference: false,
  selected_measure_id: '' as string,
  output_t: null as number | null,
})
const deviceForm = reactive({
  name: '',
  device_type: '球磨机',
  power_kw: null as number | null,
  hours: null as number | null,
  baseline_kwh: null as number | null,
  output_t: null as number | null,
})
const editingDeviceId = ref<string | null>(null)
const rfForm = reactive({
  name: '',
  device_id: '',
  method_code: '' as string,
  power_drop_kw: null as number | null,
  saving_fraction: null as number | null,
  investment_cny: null as number | null,
  model_inputs: {} as Record<string, number | null>,
})
const editingMeasureId = ref<string | null>(null)
const err = ref('')
const okMsg = ref('')
const trialMsg = ref('')
const mill = computed(() => state.results?.mill_scenario)
const pump = computed(() => state.results?.pump_scenario)
const ba = computed(() => state.results?.before_after)
const devices = computed(() => state.results?.case?.devices || [])
const copiedId = ref('')
async function copyDeviceId(id: string) {
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(id)
    copiedId.value = id
  } catch {
    copiedId.value = id
  }
}
const removedDevices = computed(() => state.results?.case?.removed_devices || [])
const showRecycleBin = ref(false)
const equipmentOptions = ref<any[]>([])
const methodOptions = ref<any[]>([])
const selectedEquipMeta = computed(() =>
  equipmentOptions.value.find(
    (o) => o.display_name === deviceForm.device_type || (o.aliases || []).includes?.(deviceForm.device_type),
  ),
)
const retrofits = computed(() => state.results?.case?.retrofits || [])
const unitProduct = computed(() => state.results?.unit_product)
const enterprise = computed(() => state.results?.enterprise_devices)
const isDemo = computed(() => !!state.results?.case?.is_demo)
const sa = computed(() => state.results?.selected_assessment || state.results?.assessment_snapshot?.selected_assessment)

const selectedDevice = computed(() => devices.value.find((d: any) => d.id === rfForm.device_id) || null)
const resolvedType = ref<any>(null)
const suppressDeviceWatch = ref(false)
const selectedMethodMeta = computed(() => methodOptions.value.find((m) => m.method_code === rfForm.method_code) || null)
const isProfessional = computed(() => !!selectedMethodMeta.value?.is_professional)
const showCaseMillPump = computed(() => isDemo.value) // 金徽案例模式保留球磨/水泵大表单

const availableMethodsZh = computed(() => {
  if (!methodOptions.value.length) return '先选择设备，我们会显示适合它的测算方法。'
  const parts: string[] = []
  for (const m of methodOptions.value) {
    if (m.is_professional) parts.push(m.label_zh || m.label || '专业模型')
    else if (m.method_code === 'baseline_fraction') parts.push('通用比例情景')
    else parts.push(m.label_zh || m.label || '通用功率差情景')
  }
  return '目前可用：' + Array.from(new Set(parts)).join('／')
})

watch(() => state.results?.case?.scenario, (sc) => {
  if (!sc) return
  form.hours = sc.hours
  form.power_drop_kw = sc.power_drop_kw
  form.baseline_power_kw = sc.baseline_power_kw
  form.pump_baseline_kwh = sc.pump_baseline_kwh
  form.use_demo_baseline = !!sc.use_demo_baseline
  form.use_jinhui_reference = !!sc.use_jinhui_reference
  form.selected_measure_id = sc.selected_measure_id || ''
  form.output_t = sc.output_t
}, { immediate: true })

watch(() => rfForm.device_id, async (id) => {
  if (suppressDeviceWatch.value) return
  methodOptions.value = []
  rfForm.method_code = ''
  rfForm.model_inputs = {}
  trialMsg.value = ''
  resolvedType.value = null
  if (!id) return
  const dev = devices.value.find((d: any) => d.id === id)
  try {
    const detail = await api.equipmentResolve(dev?.device_type || '', dev?.name || '')
    resolvedType.value = detail
    if (detail?.methods?.length) {
      methodOptions.value = detail.methods
    } else if (detail?.type_code) {
      const data = await api.equipmentMethods(detail.type_code)
      methodOptions.value = data.methods || []
    }
    if (methodOptions.value.length) {
      rfForm.method_code = methodOptions.value[0].method_code
      initModelInputs()
    }
  } catch (e: any) {
    err.value = e.message || '加载设备方法失败'
  }
})

watch(() => rfForm.method_code, () => {
  initModelInputs()
  trialMsg.value = ''
})

function initModelInputs() {
  const fields = selectedMethodMeta.value?.fields || []
  const next: Record<string, number | null> = {}
  for (const f of fields) {
    next[f.key] = f.default != null ? Number(f.default) : null
  }
  rfForm.model_inputs = next
  // 通用方法清空专业输入
  if (!selectedMethodMeta.value?.is_professional) {
    rfForm.model_inputs = {}
  }
}

function fmt(n: number | null | undefined, digits = 0) {
  if (n == null || Number.isNaN(n)) return null
  return Number(n).toLocaleString('zh-CN', { maximumFractionDigits: digits })
}

function fmtRemovedAt(iso: string | null | undefined) {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    return d.toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

function methodLabel(code: string | null | undefined) {
  if (!code) return '—'
  const hit = methodOptions.value.find((m) => m.method_code === code)
  if (hit) return hit.label_zh || hit.label || code
  const map: Record<string, string> = {
    power_drop_hours: '按功率变化估算',
    baseline_fraction: '按改造前用电量估算',
    pump_hydraulic: '专业水力模型（需要工况数据）',
    fan_pressure_flow: '专业风量风压效率模型（需要工况数据）',
    motor_efficiency: '专业电机效率模型（需要轴功率）',
    compressor_power_drop: '空压机实测功率差情景',
    transformer_losses: '变压器损耗简化模型',
  }
  return map[code] || code
}

async function recalculate() {
  err.value = ''
  try {
    const body: Record<string, number | boolean | null | string> = {
      use_demo_baseline: form.use_demo_baseline,
      use_jinhui_reference: form.use_jinhui_reference,
    }
    if (form.hours != null) body.hours = form.hours
    if (form.power_drop_kw != null) body.power_drop_kw = form.power_drop_kw
    if (form.selected_measure_id) body.selected_measure_id = form.selected_measure_id
    if (form.output_t != null) body.output_t = form.output_t
    if (form.use_demo_baseline) {
      body.baseline_power_kw = form.baseline_power_kw ?? 2400
    } else if (form.baseline_power_kw != null) {
      body.baseline_power_kw = form.baseline_power_kw
    } else {
      body.clear_baseline_power_kw = true as any
    }
    if (form.pump_baseline_kwh != null) body.pump_baseline_kwh = form.pump_baseline_kwh
    const r = await api.updateScenario(body as any)
    applyPayload(r)
    await refreshAll()
  } catch (e: any) {
    err.value = e.message
  }
}

function presetHours(h: number) {
  form.hours = h
  recalculate()
}

async function usePumpDemo() {
  form.pump_baseline_kwh = 200000
  await recalculate()
}

async function addDevice() {
  err.value = ''
  try {
    const r = await api.addDevice({
      name: deviceForm.name || '新设备',
      device_type: deviceForm.device_type,
      power_kw: deviceForm.power_kw,
      hours: deviceForm.hours,
      baseline_kwh: deviceForm.baseline_kwh,
      output_t: deviceForm.output_t,
      data_type: '假设',
    })
    applyPayload(r)
    await refreshAll()
    okMsg.value = '设备已保存。继续补充运行数据后，再计算改造效果。'
    deviceForm.name = ''
  } catch (e: any) {
    err.value = e.message
  }
}

async function removeDevice(id: string) {
  err.value = ''
  try {
    const r = await api.deleteDevice(id)
    applyPayload(r)
    await refreshAll()
  } catch (e: any) {
    err.value = e.message
  }
}

async function restoreDevice(id: string) {
  err.value = ''
  try {
    const r = await api.restoreDevice(id)
    applyPayload(r)
    await refreshAll()
  } catch (e: any) {
    err.value = e.message
  }
}

function startEditDevice(d: any) {
  editingDeviceId.value = d.id
  deviceForm.name = d.name
  deviceForm.device_type = d.device_type
  deviceForm.power_kw = d.power_kw
  deviceForm.hours = d.hours
  deviceForm.baseline_kwh = d.baseline_kwh
  deviceForm.output_t = d.output_t
}

function cancelEditDevice() {
  editingDeviceId.value = null
  deviceForm.name = ''
  deviceForm.power_kw = null
  deviceForm.hours = null
  deviceForm.baseline_kwh = null
  deviceForm.output_t = null
}

async function saveDevice() {
  err.value = ''
  if (!editingDeviceId.value) {
    await addDevice()
    return
  }
  try {
    const body: Record<string, unknown> = {
      name: deviceForm.name,
      device_type: deviceForm.device_type,
    }
    const optionalNum = [
      ['power_kw', 'clear_power_kw'],
      ['hours', 'clear_hours'],
      ['baseline_kwh', 'clear_baseline_kwh'],
      ['output_t', 'clear_output_t'],
    ] as const
    for (const [field, flag] of optionalNum) {
      const raw = (deviceForm as any)[field]
      if (raw === '' || raw === null || raw === undefined) {
        body[flag] = true
      } else {
        body[field] = Number(raw)
      }
    }
    const r = await api.updateDevice(editingDeviceId.value, body)
    applyPayload(r)
    await refreshAll()
    cancelEditDevice()
  } catch (e: any) {
    err.value = e.message
  }
}


function originBadge(r: any) {
  if (!r) return '待核实'
  if (r.provenance_label) return r.provenance_label
  if (String(r.source || '').includes('复制')) return '待核实'
  if (r.lifecycle_status === 'draft') return '待核实'
  const sels = (state.results?.case?.reference_selections || []).filter((s: any) =>
    s.measure_id === r.id && (s.active || s.applied) && !s.revoked && !s.superseded)
  if (sels.length) return '参考试算'
  if (r.data_type === '披露' || r.data_type === '文献') return '公开资料'
  if (r.data_type === '实测' || r.data_type === '用户提供·未核验' || r.data_type === '企业提供·未核验' || r.data_type === '假设') return '用户输入'
  return '待核实'
}

function deviceOriginBadge(d: any) {
  if (!d) return '待核实'
  if (d.data_type === '披露' || d.data_type === '文献') return '公开资料'
  if (d.data_type === '实测' || d.data_type === '用户提供·未核验' || d.data_type === '企业提供·未核验' || d.data_type === '假设') return '用户输入'
  return '待核实'
}

function isReferenceAssumption(f: any) {
  if (f?.default == null) return false
  const v = rfForm.model_inputs[f.key]
  return v != null && Number(v) === Number(f.default)
}

async function confirmPumpType(opt: any) {
  const dev = selectedDevice.value
  if (!dev) return
  try {
    const r = await api.updateDevice(dev.id, { device_type: opt.display_name })
    applyPayload(r)
    await refreshAll()
    rfForm.device_id = dev.id
    const detail = await api.equipmentResolve(opt.display_name, dev.name)
    resolvedType.value = detail
    methodOptions.value = detail.methods || []
  } catch (e: any) {
    err.value = e.message
  }
}

async function startEditMeasure(r: any) {
  editingMeasureId.value = r.id
  suppressDeviceWatch.value = true
  rfForm.name = r.name
  rfForm.device_id = r.device_id
  try {
    const dev = devices.value.find((d: any) => d.id === r.device_id)
    const detail = await api.equipmentResolve(dev?.device_type || '', dev?.name || '')
    resolvedType.value = detail
    methodOptions.value = detail.methods || []
  } catch { /* keep going */ }
  rfForm.method_code = r.method_code || r.calculation_method || ''
  rfForm.power_drop_kw = r.power_drop_kw
  rfForm.saving_fraction = r.saving_fraction
  rfForm.investment_cny = r.investment_cny
  rfForm.model_inputs = { ...(r.model_inputs || {}) }
  suppressDeviceWatch.value = false
}

async function saveMeasure() {
  if (editingMeasureId.value) {
    err.value = ''
    try {
      const body: Record<string, unknown> = {
        name: rfForm.name || '技改措施',
        device_id: rfForm.device_id,
        method_code: rfForm.method_code,
        calculation_method: rfForm.method_code,
        investment_cny: rfForm.investment_cny,
        select: true,
      }
      if (isProfessional.value) body.model_inputs = buildModelInputsPayload()
      else if (rfForm.method_code === 'power_drop_hours') body.power_drop_kw = rfForm.power_drop_kw
      else if (rfForm.method_code === 'baseline_fraction') body.saving_fraction = rfForm.saving_fraction
      const r = await api.updateRetrofit(editingMeasureId.value, body)
      applyPayload(r)
      await refreshAll()
      editingMeasureId.value = null
      okMsg.value = (r && r.message) || '已保存，节电与减碳结果已更新'
    } catch (e: any) {
      err.value = '未保存，原结果保留，原因：' + (e.message || '输入无效')
    }
    return
  }
  await addRetrofit()
}

async function removeMeasure(id: string) {
  if (!confirm('删除这项措施？可在需要时重新创建。')) return
  try {
    const r = await api.deleteRetrofit(id)
    applyPayload(r)
    await refreshAll()
    okMsg.value = '措施已删除。'
  } catch (e: any) {
    err.value = e.message
  }
}

async function copyMeasure(id: string) {
  try {
    const r = await api.copyRetrofit(id)
    applyPayload(r)
    await refreshAll()
    okMsg.value = (r && r.message) || '已复制参数。请核对新设备是否适用，确认前只作参考试算。'
  } catch (e: any) {
    err.value = e.message
  }
}

async function revertMeasure(id: string) {
  try {
    const r = await api.revertRetrofit(id)
    applyPayload(r)
    await refreshAll()
    okMsg.value = '已恢复到上一版本。'
  } catch (e: any) {
    err.value = e.message
  }
}

function buildModelInputsPayload(): Record<string, number> | undefined {
  if (!isProfessional.value) return undefined
  const out: Record<string, number> = {}
  const fields = selectedMethodMeta.value?.fields || []
  for (const f of fields) {
    const v = rfForm.model_inputs[f.key]
    if (v == null || Number.isNaN(Number(v))) {
      if (f.required) throw new Error(`请填写：${f.label}`)
      continue
    }
    out[f.key] = Number(v)
  }
  return out
}

async function trialCalculate() {
  err.value = ''
  trialMsg.value = ''
  if (!isProfessional.value || !rfForm.method_code) {
    trialMsg.value = '请先选择专业模型并填写工况。'
    return
  }
  try {
    const inputs = buildModelInputsPayload() || {}
    const r = await api.equipmentCalculate({
      method_code: rfForm.method_code,
      ...inputs,
      device_hours: selectedDevice.value?.hours ?? null,
    })
    if (r.status === 'ok') {
      const dir = r.energy_direction === 'increase' ? '预计增加用电' : '试算节电约'
      trialMsg.value = `${dir} ${fmt(r.increased_kwh ?? r.saved_kwh, 2)} kWh` + (r.assumptions?.length ? `｜${r.assumptions.join('；')}` : '')
      if (r.hours_conflict_message) trialMsg.value = r.hours_conflict_message
    } else {
      trialMsg.value = r.error || r.message || '试算未通过'
    }
  } catch (e: any) {
    trialMsg.value = e.message || '试算失败'
  }
}

async function addRetrofit() {
  err.value = ''
  if (!rfForm.device_id) {
    err.value = '请先选择目标设备'
    return
  }
  if (!rfForm.method_code) {
    err.value = '请选择测算方法'
    return
  }
  try {
    const body: Record<string, unknown> = {
      name: rfForm.name || '新技改',
      device_id: rfForm.device_id,
      method_code: rfForm.method_code,
      calculation_method: rfForm.method_code,
      investment_cny: rfForm.investment_cny,
      select: true,
    }
    if (isProfessional.value) {
      body.model_inputs = buildModelInputsPayload()
      body.power_drop_kw = null
      body.saving_fraction = null
    } else if (rfForm.method_code === 'power_drop_hours') {
      body.power_drop_kw = rfForm.power_drop_kw
      body.saving_fraction = null
    } else if (rfForm.method_code === 'baseline_fraction') {
      body.saving_fraction = rfForm.saving_fraction
      body.power_drop_kw = null
    }
    const r = await api.addRetrofit(body)
    applyPayload(r)
    await refreshAll()
    rfForm.name = ''
    okMsg.value = (r && r.message) || '已保存，节电与减碳结果已更新'
  } catch (e: any) {
    err.value = e.message
  }
}

async function selectMeasure(id: string) {
  form.selected_measure_id = id
  await recalculate()
}

onMounted(async () => {
  try {
    const data = await api.equipmentTypes()
    equipmentOptions.value = (data.types || []).map((t: any) => ({ ...t, display_name: t.display_name }))
  } catch (e) {
    equipmentOptions.value = [
      { display_name: '球磨机', type_code: 'ball_mill', model_status: 'supported', supported_methods: ['power_drop_hours'], can_register: true, can_trial_calc: true, in_decision_flow: true },
      { display_name: '矿井排水泵', type_code: 'pump_drainage', model_status: 'supported', supported_methods: ['baseline_fraction', 'pump_hydraulic'], can_register: true, can_trial_calc: true, in_decision_flow: true },
      { display_name: '循环水泵/矿浆泵', type_code: 'pump_process', model_status: 'supported', supported_methods: ['baseline_fraction', 'pump_hydraulic'], can_register: true, can_trial_calc: true, in_decision_flow: true },
      { display_name: '矿井通风机', type_code: 'fan_ventilation', model_status: 'supported', supported_methods: ['power_drop_hours', 'fan_pressure_flow'], can_register: true, can_trial_calc: true, in_decision_flow: true },
      { display_name: '空气压缩机', type_code: 'compressor', model_status: 'reference_scenario', supported_methods: ['baseline_fraction', 'compressor_power_drop'], can_register: true, can_trial_calc: true, in_decision_flow: true },
      { display_name: '高效电动机/驱动', type_code: 'motor_drive', model_status: 'supported', supported_methods: ['motor_efficiency'], can_register: true, can_trial_calc: true, in_decision_flow: true },
      { display_name: '配电变压器', type_code: 'transformer', model_status: 'supported', supported_methods: ['transformer_losses'], can_register: true, can_trial_calc: true, in_decision_flow: true },
      { display_name: '其他设备（仅录入/通用）', type_code: 'generic', model_status: 'reference_scenario', supported_methods: ['baseline_fraction'], can_register: true, can_trial_calc: true, in_decision_flow: true },
    ]
  }
})
</script>

<template>
  <div class="notice">步骤：①选一台设备 → ②选择改造办法 → ③填已知数据/选参考条件 → ④查看结果</div>

  <p class="notice" v-if="okMsg" style="border-color:#555">{{ okMsg }}</p>
  <p class="notice warn" v-if="err">{{ err }}</p>
  <div class="eyebrow">设备与改造</div>
  <h1>想先看看哪台设备能省电？</h1>
  <p class="lead">先选设备，我们会显示适合它的测算方法。有流量、扬程或效率记录？试试更贴近工况的测算。</p>

  <div class="section" style="border-top:none;margin-top:0;padding-top:0">
    <h2>我的设备</h2>
    <p class="notice">可随时添加、编辑设备。删掉的设备会进入回收站，可随时找回。</p>
    <div class="btn-row" style="margin-bottom:10px">
      <button class="btn secondary" type="button" @click="showRecycleBin = !showRecycleBin">
        {{ showRecycleBin ? '收起回收站' : '打开回收站' }}{{ removedDevices.length ? `（${removedDevices.length}）` : '' }}
      </button>
    </div>

    <!-- 窄屏卡片列表（P1-05） -->
    <div class="device-card-list" v-if="devices.length">
      <div class="device-card" v-for="d in devices" :key="d.id">
        <div class="device-card-title">{{ d.name }} <span class="tag">{{ deviceOriginBadge(d) }}</span></div>
        <div class="device-card-meta">
          <span>{{ d.device_type }}</span>
          <span>{{ d.power_kw == null ? '功率待填' : d.power_kw + ' kW' }}</span>
          <span>{{ d.hours == null ? '小时待填' : d.hours + ' h' }}</span>
          <span class="mono">设备ID {{ d.id }}</span>
        </div>
        <div class="btn-row device-card-actions">
          <button class="btn secondary" type="button" @click="copyDeviceId(d.id)">{{ copiedId === d.id ? '已复制ID' : '复制设备ID' }}</button>
          <button class="btn secondary" type="button" @click="startEditDevice(d)">编辑</button>
          <button class="btn secondary" type="button" @click="removeDevice(d.id)">删除</button>
          <button class="btn secondary" type="button" @click="rfForm.device_id = d.id">选为改造对象</button>
        </div>
      </div>
    </div>
    <div class="table-scroll" v-if="devices.length">
      <table class="data device-table-wide">
        <thead>
          <tr><th>名称</th><th>类型</th><th>设备ID</th><th>来源</th><th>功率</th><th>小时</th><th>基准kWh</th><th>产量t</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="d in devices" :key="'tbl-' + d.id">
            <td>{{ d.name }}</td>
            <td>{{ d.device_type }}</td>
            <td class="mono">
              {{ d.id }}
              <button class="btn secondary" type="button" @click="copyDeviceId(d.id)">{{ copiedId === d.id ? '已复制' : '复制' }}</button>
            </td>
            <td><span class="tag">{{ deviceOriginBadge(d) }}</span></td>
            <td class="num">{{ d.power_kw == null ? '待填写' : d.power_kw }}</td>
            <td class="num">{{ d.hours == null ? '待填写' : d.hours }}</td>
            <td class="num">{{ d.baseline_kwh == null ? '—' : d.baseline_kwh }}</td>
            <td class="num">{{ d.output_t == null ? '—' : d.output_t }}</td>
            <td class="btn-row">
              <button class="btn secondary" type="button" @click="startEditDevice(d)">编辑</button>
              <button class="btn secondary" type="button" @click="removeDevice(d.id)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
      <p class="muted table-scroll-hint">宽表可左右滑动查看</p>
    </div>
    <p class="muted" v-else>暂无设备。请用下方表单添加；若刚删过，可打开回收站找回。</p>
    <p class="muted" v-if="enterprise?.skipped?.length">汇总已跳过缺功率/小时的设备：{{ enterprise.skipped.map((s: any) => s.name).join('、') }}</p>

    <div class="card" v-if="showRecycleBin || removedDevices.length" style="margin-top:12px;border-color:#E5E5E5">
      <h3>回收站</h3>
      <p class="muted">这里是已删除的设备。点「恢复」即可回到上方设备列表。</p>
      <div class="device-card-list" v-if="removedDevices.length">
        <div class="device-card" v-for="d in removedDevices" :key="'rm-' + d.id">
          <div class="device-card-title">{{ d.name }} <span class="tag">{{ deviceOriginBadge(d) }}</span></div>
          <div class="device-card-meta">
            <span>{{ d.device_type }}</span>
            <span>{{ fmtRemovedAt(d.removed_at) }}</span>
          </div>
          <button class="btn" type="button" @click="restoreDevice(d.id)">恢复</button>
        </div>
      </div>
      <p class="muted" v-else>回收站是空的。</p>
    </div>

    <div class="card" style="margin-top:12px;border:1px solid #E5E5E5;background:#fff">
      <h3>{{ editingDeviceId ? '编辑设备' : '添加一台设备' }}</h3>
      <p class="muted">先选设备，我们会显示适合它的测算方法。功率、小时等可稍后补全。</p>
      <div class="twocol">
        <label class="field">名称<input v-model="deviceForm.name" placeholder="如：1#球磨机" /></label>
        <label class="field">类型
          <select v-model="deviceForm.device_type">
            <option v-for="opt in equipmentOptions" :key="opt.type_code" :value="opt.display_name">
              {{ opt.display_name }}
            </option>
          </select>
          <p class="muted" v-if="selectedEquipMeta" style="grid-column:1/-1">
            可录入：{{ selectedEquipMeta.can_register ? '是' : '否' }}
            ｜可独立试算：{{ selectedEquipMeta.can_trial_calc ? '是' : '否' }}
            ｜已接入完整决策流程：{{ selectedEquipMeta.in_decision_flow ? '是' : '否' }}
            <span v-if="selectedEquipMeta.safety_notes">｜{{ selectedEquipMeta.safety_notes }}</span>
          </p>
        </label>
        <label class="field">功率 kW<input type="number" v-model.number="deviceForm.power_kw" placeholder="可留空" /></label>
        <label class="field">年实际运行小时<input type="number" v-model.number="deviceForm.hours" placeholder="可留空" /></label>
        <label class="field">基准年电耗 kWh<input type="number" v-model.number="deviceForm.baseline_kwh" /></label>
        <label class="field">处理量 t<input type="number" v-model.number="deviceForm.output_t" /></label>
      </div>
      <div class="btn-row">
        <button class="btn" type="button" @click="saveDevice">{{ editingDeviceId ? '保存修改' : '添加设备' }}</button>
        <button class="btn secondary" type="button" v-if="editingDeviceId" @click="cancelEditDevice">取消编辑</button>
      </div>
    </div>
  </div>

  <div class="section">
    <h2>准备给这台设备做什么改造？</h2>
    <p class="notice warn" v-if="devices.length && !retrofits.length">请先为设备添加改造方案，才能计算节电与减排。</p>
    <p class="muted" v-if="!rfForm.device_id">先选择设备，我们会自动显示适用参数。</p>

    <div class="card" style="margin-top:8px">
      <div class="twocol">
        <label class="field">目标设备
          <select v-model="rfForm.device_id">
            <option value="">请选择</option>
            <option v-for="d in devices" :key="d.id" :value="d.id">{{ d.name }}（{{ d.device_type }}｜{{ d.id }}）</option>
          </select>
        </label>
        <label class="field">措施名称<input v-model="rfForm.name" placeholder="如：风机效率提升" /></label>
        <label class="field" style="grid-column:1/-1">测算方法
          <select v-model="rfForm.method_code" :disabled="!methodOptions.length">
            <option value="" disabled>{{ methodOptions.length ? '请选择' : '请先选择设备' }}</option>
            <option v-for="m in methodOptions" :key="m.method_code" :value="m.method_code">
              {{ m.label_zh || m.label }}
            </option>
          </select>
          <p class="muted">{{ availableMethodsZh }}</p>
          <p class="muted" v-if="rfForm.method_code">我们已根据设备类型列出可用的测算方法。请选择一种，再填写相应的运行条件。</p>
          <p class="muted" v-if="selectedMethodMeta?.ui_hint">{{ selectedMethodMeta.ui_hint }}</p>
        </label>
        <div v-if="resolvedType?.needs_confirm" class="notice warn" style="grid-column:1/-1">
          {{ resolvedType.message }}
          <div class="btn-row" style="margin-top:8px">
            <button class="btn secondary" type="button" v-for="opt in resolvedType.confirm_options" :key="opt.type_code" @click="confirmPumpType(opt)">
              确认为{{ opt.display_name }}
            </button>
          </div>
        </div>
      </div>

      <!-- 通用：功率差 -->
      <div class="twocol" v-if="rfForm.method_code === 'power_drop_hours'">
        <label class="field">功率降低 kW<input type="number" v-model.number="rfForm.power_drop_kw" /></label>
        <label class="field">投资（可选）元<input type="number" v-model.number="rfForm.investment_cny" /></label>
      </div>
      <!-- 通用：比例 -->
      <div class="twocol" v-else-if="rfForm.method_code === 'baseline_fraction'">
        <label class="field">节电比例 0–1<input type="number" step="0.0001" v-model.number="rfForm.saving_fraction" /></label>
        <label class="field">投资（可选）元<input type="number" v-model.number="rfForm.investment_cny" /></label>
        <p class="muted" style="grid-column:1/-1">只有设备用电量？也可以先做初步估算。请在设备上填写改造前一年用电量。</p>
      </div>
      <!-- 专业动态字段 -->
      <div class="twocol" v-else-if="isProfessional">
        <label class="field" v-for="f in (selectedMethodMeta?.fields || [])" :key="f.key">
          {{ f.label }}{{ f.unit ? `（${f.unit}）` : '' }}{{ f.required ? '' : '（可选）' }}
          <span class="tag" v-if="isReferenceAssumption(f)" style="margin-left:6px">参考假设</span>
          <input type="number" step="any" v-model.number="rfForm.model_inputs[f.key]" />
          <span class="muted" v-if="isReferenceAssumption(f)">这个数值是演示用的参考假设，不是你的设备实测数据。请核对后再用于测算。</span>
        </label>
        <label class="field">投资（可选）元<input type="number" v-model.number="rfForm.investment_cny" /></label>
      </div>

      <div class="btn-row" style="margin-top:10px">
        <button class="btn" type="button" @click="saveMeasure">{{ editingMeasureId ? '保存修改并重算' : '创建措施并设为当前' }}</button>
        <button class="btn secondary" type="button" v-if="isProfessional" @click="trialCalculate">先独立试算</button>
      </div>
      <p class="muted" v-if="trialMsg">{{ trialMsg }}</p>
    </div>

    <div v-if="retrofits.length" class="measure-list" style="margin-top:12px">
      <div class="device-card-list">
        <div class="device-card" v-for="r in retrofits" :key="'mc-' + r.id" :class="{ active: form.selected_measure_id === r.id }">
          <div class="device-card-title">{{ r.name }}</div>
          <div class="device-card-meta">
            <span>目标：{{ r.target_device }}</span>
            <span>{{ methodLabel(r.method_code || r.calculation_method) }}</span>
            <span class="tag">{{ originBadge(r) }}</span>
            <span>投资 {{ r.investment_cny ?? '—' }}</span>
          </div>
          <div class="btn-row device-card-actions">
            <button class="btn secondary" type="button" @click="selectMeasure(r.id)">设为当前</button>
            <button class="btn secondary" type="button" @click="startEditMeasure(r)">编辑</button>
            <button class="btn secondary" type="button" @click="copyMeasure(r.id)">复制</button>
            <button class="btn secondary" type="button" @click="removeMeasure(r.id)">删除</button>
            <button class="btn secondary" type="button" v-if="r.previous_snapshot" @click="revertMeasure(r.id)">恢复上一版</button>
          </div>
        </div>
      </div>
      <div class="table-scroll">
        <table class="data device-table-wide">
          <thead>
            <tr><th>名称</th><th>目标设备</th><th>测算方法</th><th>来源</th><th>投资</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="r in retrofits" :key="r.id" :class="{ active: form.selected_measure_id === r.id }">
              <td>{{ r.name }}</td>
              <td>{{ r.target_device }}</td>
              <td>{{ methodLabel(r.method_code || r.calculation_method) }}</td>
              <td><span class="tag">{{ originBadge(r) }}</span></td>
              <td class="num">{{ r.investment_cny ?? '—' }}</td>
              <td class="btn-row">
                <button class="btn secondary" type="button" @click="selectMeasure(r.id)">设为当前</button>
                <button class="btn secondary" type="button" @click="startEditMeasure(r)">编辑</button>
                <button class="btn secondary" type="button" @click="copyMeasure(r.id)">复制</button>
                <button class="btn secondary" type="button" @click="removeMeasure(r.id)">删除</button>
                <button class="btn secondary" type="button" v-if="r.previous_snapshot" @click="revertMeasure(r.id)">恢复上一版</button>
              </td>
            </tr>
          </tbody>
        </table>
        <p class="muted table-scroll-hint">宽表可左右滑动查看，窄屏请使用上方卡片操作</p>
      </div>
    </div>

    <div class="card" v-if="sa" style="margin-top:12px">
      <h3>当前措施结果</h3>
      <div class="label">{{ sa.display_energy_label || '预计一年省电' }}</div>
      <div class="metric" :class="{ pending: sa.saved_kwh == null }">
        {{ sa.saved_kwh != null ? fmt(sa.energy_direction === 'increase' ? (sa.increased_kwh ?? Math.abs(sa.saved_kwh)) : sa.saved_kwh) + ' kWh' : (sa.waiting_message || '尚待补充') }}
      </div>
      <div class="label" style="margin-top:8px">{{ sa.display_carbon_label || '电力相关减排' }}</div>
      <div class="metric" :class="{ pending: sa.co2_reduction_t == null }">
        {{ sa.co2_reduction_t != null ? fmt(sa.energy_direction === 'increase' ? Math.abs(sa.co2_reduction_t) : sa.co2_reduction_t, 3) + ' tCO₂' : '—' }}
      </div>
      <p class="notice warn" v-if="sa.hours_conflict_message">{{ sa.hours_conflict_message }}</p>
      <p class="notice warn" v-if="sa.safety_unverified">{{ sa.safety_note || '还没有核实矿井最低安全风量。可以先查看节能潜力，暂不推荐具体改造方案。' }}</p>
      <p class="muted" v-if="sa.cost_note && sa.annual_cost_saving_cny == null">{{ sa.cost_note }}</p>
      <p class="muted" v-if="sa.safety_note && !sa.safety_unverified">{{ sa.safety_note }}</p>
      <p class="muted">模型：{{ sa.model_label || '—' }}</p>
    </div>
  </div>

  <!-- 金徽案例模式：保留球磨/水泵大表单；其他企业不强制展示（P1-04） -->
  <div class="section" v-if="showCaseMillPump">
    <h2>球磨机工况（金徽公开案例）</h2>
    <div class="twocol">
      <div class="card">
        <span class="tag tag-abs">公开案例</span>
        <label class="field">功率降低 kW
          <input type="number" v-model.number="form.power_drop_kw" />
        </label>
        <label class="field">年有效小时
          <input type="number" v-model.number="form.hours" />
        </label>
        <div class="btn-row">
          <button class="btn secondary" type="button" @click="presetHours(2000)">2000h</button>
          <button class="btn secondary" type="button" @click="presetHours(4000)">4000h</button>
          <button class="btn secondary" type="button" @click="presetHours(6000)">6000h</button>
          <button class="btn" type="button" @click="recalculate">重新计算</button>
        </div>
      </div>
      <div class="card">
        <span class="tag tag-abs">情景估算</span>
        <div v-if="mill?.status === 'ok' || sa?.saved_kwh != null">
          <div class="label">节电</div>
          <div class="metric">{{ fmt((sa?.technology_type || '').startsWith('professional') ? sa?.saved_kwh : (mill?.saved_kwh ?? sa?.saved_kwh)) }} <span class="unit">kWh</span></div>
          <div class="label" style="margin-top:10px">改造前绝对能耗</div>
          <div class="metric" :class="{ pending: ba?.before_kwh == null }">
            {{ ba?.before_kwh != null ? fmt(ba.before_kwh) + ' kWh' : (ba?.display_before || '尚无基准功率，无法确定') }}
          </div>
          <div class="label" style="margin-top:10px">改造后绝对能耗</div>
          <div class="metric" :class="{ pending: ba?.after_kwh == null }">
            {{ ba?.after_kwh != null ? fmt(ba.after_kwh) + ' kWh' : (ba?.display_after || '待输入') }}
          </div>
          <p class="muted" style="margin-top:10px">{{ ba?.note }}</p>
          <div class="label" style="margin-top:10px">单位产品电耗</div>
          <div v-if="unitProduct?.status === 'ok'" class="metric">{{ fmt(unitProduct.kwh_per_t, 2) }} <span class="unit">kWh/t</span></div>
          <div v-else class="notice warn">{{ unitProduct?.message || '待填写处理量' }}</div>
        </div>
        <div v-else class="notice warn">{{ mill?.error || mill?.message || state.results?.mill_error || '暂无结果' }}</div>
      </div>
    </div>
  </div>

  <div class="section" v-if="showCaseMillPump">
    <h2>水泵改造（金徽公开案例）</h2>
    <div class="twocol">
      <div class="card">
        <span class="tag tag-abs">用户填写</span>
        <label class="field">设备基准年电耗 kWh
          <input type="number" v-model.number="form.pump_baseline_kwh" placeholder="待输入设备基准年电耗" />
        </label>
        <p class="muted">节电比例取自绑定到泵设备的措施；不能用公司总用电代替单台泵基准。</p>
        <div class="btn-row">
          <button class="btn" type="button" @click="recalculate">重新计算</button>
          <button class="btn secondary" type="button" @click="usePumpDemo">使用测试基准 200,000 kWh</button>
        </div>
      </div>
      <div class="card">
        <span class="tag tag-abs">情景估算</span>
        <div v-if="pump?.status === 'ok'">
          <div class="label">泵节电</div>
          <div class="metric">{{ fmt(pump.saved_kwh) }} <span class="unit">kWh</span></div>
          <div class="label" style="margin-top:10px">对应减排</div>
          <div class="metric">{{ fmt(pump.co2_reduction_t, 2) }} <span class="unit">tCO₂</span></div>
          <p class="muted">措施 {{ pump.measure_id }}</p>
        </div>
        <div v-else class="notice warn">{{ pump?.message || pump?.error || '待输入设备基准年电耗' }}</div>
      </div>
    </div>
  </div>

  <div v-if="err" class="error">{{ err }}</div>
  <MethodPanel :mill="sa || mill" />
</template>

<style scoped>
.device-card-list { display: none; gap: 10px; }
.device-card {
  border: 1px solid #E5E5E5;
  border-radius: 10px;
  padding: 12px;
  background: #fff;
}
.device-card-title { font-weight: 600; margin-bottom: 6px; word-break: normal; overflow-wrap: break-word; }
.device-card-meta { display: flex; flex-wrap: wrap; gap: 8px 12px; color: #555; font-size: 13px; margin-bottom: 8px; }
.device-card-meta > span { white-space: nowrap; }
.device-card.active { border-color: #111; }
.device-card-actions .btn { min-height: 36px; min-width: 64px; white-space: nowrap; }
.table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
.table-scroll-hint { display: none; font-size: 12px; }
@media (max-width: 390px) {
  .device-card { overflow: hidden; }
  .device-card-actions .btn { min-height: 40px; }
}
@media (max-width: 768px) {
  .device-card-list { display: grid; }
  .device-table-wide { display: none; }
  .table-scroll-hint { display: block; }
}
</style>
