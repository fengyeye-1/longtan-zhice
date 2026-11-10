<script setup lang="ts">
import { ref, computed } from 'vue'
import { state } from '../stores/app'
import { api } from '../api/client'
import MethodPanel from '../components/MethodPanel.vue'
import { safeHref } from '../api/safeHref'

const emit = defineEmits<{ go: [string] }>()
const preview = ref('')
const loading = ref(false)
const preset = ref('full')
const customSections = ref(['summary', 'energy', 'carbon', 'roi', 'quotes', 'methods'])
const exportStatus = ref('')
const exportMsg = ref('')
const previewError = ref('')

function isActiveQuote(q: any) {
  if (!q) return false
  if (q.active === false || q.superseded) return false
  return true
}
const currentQuote = computed(() => {
  const snapQ = state.results?.assessment_snapshot?.quote_choice
  if (isActiveQuote(snapQ)) return snapQ
  const caseQ = state.results?.case?.quote_choice
  if (isActiveQuote(caseQ)) return caseQ
  return null
})
const historicalQuote = computed(() => {
  if (currentQuote.value) return null
  return state.results?.assessment_snapshot?.historical_quote_choice
    || (state.results?.case?.quote_choice && !isActiveQuote(state.results?.case?.quote_choice)
      ? state.results.case.quote_choice
      : null)
})

const sa = computed(() => state.results?.selected_assessment || state.results?.assessment_snapshot?.selected_assessment)
const mill = computed(() => state.results?.mill_scenario)
const ba = computed(() => state.results?.before_after)
const prep = computed(() => state.results?.data_prep || [])
const sources = computed(() => state.results?.case?.sources || [])
const factorMeta = computed(() => state.results?.factor_meta || state.results?.defaults?.factor_meta)
const schemes = computed(() => state.results?.schemes || [])
const snap = computed(() => state.results?.assessment_snapshot)
const measureName = computed(() => sa.value?.measure_name || snap.value?.selected_measure_name || '—')
const deviceName = computed(() => sa.value?.device_name || snap.value?.selected_device_name || '—')

const sectionOptions = [
  { id: 'summary', label: '一页摘要内容' },
  { id: 'energy', label: '设备节电' },
  { id: 'carbon', label: '碳排放变化' },
  { id: 'roi', label: '投资与回本' },
  { id: 'quotes', label: '供应商报价' },
  { id: 'schemes', label: '方案对比' },
  { id: 'methods', label: '方法假设' },
  { id: 'gaps', label: '缺口限制' },
  { id: 'sources', label: '来源' },
]

function fmt(n: number | null | undefined, digits = 2) {
  if (n == null || Number.isNaN(n)) return '—'
  return Number(n).toLocaleString('zh-CN', { maximumFractionDigits: digits })
}

async function loadPreview() {
  loading.value = true
  exportStatus.value = ''
  exportMsg.value = ''
  previewError.value = ''
  if (preset.value === 'custom' && customSections.value.length === 0) {
    loading.value = false
    preview.value = ''
    previewError.value = '请至少选择一个报告章节，不能导出空报告'
    return
  }
  try {
    const body: any = { preset: preset.value, format: 'txt' }
    if (preset.value === 'custom') body.sections = customSections.value
    const r = await api.reportPreview(body)
    preview.value = r.text || ''
    exportStatus.value = r.export_status || ''
  } catch (e: any) {
    preview.value = ''
    previewError.value = e?.message || '预览失败，未改用其他版本的报告'
  } finally {
    loading.value = false
  }
}

async function exportNow(format: string) {
  exportMsg.value = ''
  if (preset.value === 'custom' && customSections.value.length === 0) {
    exportMsg.value = '请至少选择一个报告章节，不能导出空报告'
    return
  }
  try {
    const body: any = { preset: preset.value, format }
    if (preset.value === 'custom') body.sections = customSections.value
    const exported = await api.reportExport(body)
    const text = typeof exported === 'string' ? exported : (exported.text || '')
    const blob = new Blob([text], {
      type: format === 'csv' ? 'text/csv;charset=utf-8' : (format === 'html' ? 'text/html;charset=utf-8' : 'text/plain;charset=utf-8'),
    })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = format === 'csv' ? '陇碳智策_结果.csv' : (format === 'html' ? '陇碳智策_评估材料.html' : '陇碳智策_报告.txt')
    a.click()
    if (typeof exported !== 'string' && exported.archiveSaved === false) {
      exportMsg.value = '正文已下载，但历史归档未保存（' + (exported.archiveErrorCode || 'write_failed') + '）。请自行保管这份文件作为备份。'
    }
  } catch (e: any) {
    exportMsg.value = e?.message || '导出失败'
  }
}

loadPreview()
</script>

<template>
  <div class="eyebrow">我的评估报告</div>
  <h1>下载这次评估</h1>
  <p class="lead">可选完整报告、一页摘要内容或自选章节。只选报价时不会夹带未选中的设备节电结果。提供 TXT / CSV / HTML；HTML 可用浏览器打印为 PDF。没有数据的分项会写「尚待补充」。导出记录按当前企业写入服务器归档，重启后只要仍打开该企业即可查阅；计算情景指纹（snapshot_id）与归档编号（export_id）不是同一个东西。请同时在本机保存 TXT/CSV/HTML 备份：免费在线实例休眠或重部署可能丢失归档。</p>
  <p class="notice" v-if="snap?.needs_recalc">有未更新的修改，请先重新测算后再导出。</p>

  <div class="card">
    <label class="field">这次想下载哪些内容？
      <select v-model="preset" @change="loadPreview">
        <option value="full">完整报告</option>
        <option value="one_page">一页摘要内容</option>
        <option value="custom">自选章节</option>
      </select>
    </label>
    <div v-if="preset === 'custom'" class="btn-row" style="flex-wrap:wrap">
      <label v-for="s in sectionOptions" :key="s.id" style="margin-right:12px">
        <input type="checkbox" :value="s.id" v-model="customSections" @change="loadPreview" /> {{ s.label }}
      </label>
    </div>
    <div class="btn-row">
      <button class="btn" @click="exportNow('txt')">下载这次评估（TXT）</button>
      <button class="btn secondary" @click="exportNow('csv')">导出 CSV</button>
      <button class="btn secondary" @click="exportNow('html')">导出 HTML（可打印）</button>
      <button class="btn secondary" @click="loadPreview">刷新预览</button>
      <button class="btn secondary" @click="emit('go', 'help')">使用帮助</button>
    </div>
    <p class="muted" v-if="exportStatus">导出状态：{{ exportStatus }}（有模块数据不足时会标明，不会伪造数字）</p>
    <p class="error" v-if="exportMsg">{{ exportMsg }}</p>
    <p class="error" v-if="previewError">{{ previewError }}</p>
  </div>

  <div class="section">
    <h2>本次测算摘要</h2>
    <table class="data">
      <tbody>
        <tr><th>案例</th><td>{{ state.results?.case?.name }}</td></tr>
        <tr><th>算到哪里</th><td>设备节电及相应电力排放变化（不是整厂碳核算）</td></tr>
        <tr><th>设备</th><td>{{ deviceName }}</td></tr>
        <tr><th>本次改造</th><td>{{ measureName }}</td></tr>
        <tr><th>节电 kWh</th><td>{{ fmt(sa?.saved_kwh, 0) }}</td></tr>
        <tr><th>预计减少的电力相关排放 tCO₂</th><td>{{ fmt(sa?.co2_reduction_t) }}</td></tr>
        <tr><th>排放换算依据</th><td>{{ factorMeta?.label || factorMeta?.source_name || '待确认' }}（{{ state.results?.case?.scenario?.emission_factor }}）</td></tr>
        <tr><th>节费 元</th><td>{{ sa?.annual_cost_saving_cny != null ? fmt(sa.annual_cost_saving_cny) : (sa?.cost_note || '—') }}</td></tr>
        <tr><th>回收期</th><td>{{ sa?.simple_payback_years ?? sa?.payback_message ?? '—' }}</td></tr>
        <tr v-if="sa?.technology_type !== 'pump'"><th>改造前展示</th><td>{{ ba?.display_before }}</td></tr>
        <tr v-if="sa?.technology_type !== 'pump'"><th>改造后展示</th><td>{{ ba?.display_after }}</td></tr>
        <tr v-if="currentQuote"><th>当前采用报价</th><td>{{ currentQuote.label || currentQuote.bidder_full_name }}</td></tr>
        <tr v-else-if="historicalQuote"><th>参考报价</th><td>曾经引用·已失效（{{ historicalQuote.label || historicalQuote.bidder_full_name || '参考报价' }}；原因：{{ historicalQuote.invalidated_reason || '条件已变化' }}；时间：{{ historicalQuote.invalidated_at || '—' }}）</td></tr>
        <tr v-else><th>参考报价</th><td>未选</td></tr>
        <tr><th>测算时间</th><td>{{ state.results?.calculated_at }}</td></tr>
        <tr><th>计算情景指纹</th><td class="mono">{{ snap?.snapshot_id || '—' }}（仅标识本次测算输入；历史归档请用导出编号 export_id）</td></tr>
      </tbody>
    </table>
  </div>

  <div class="section" v-if="schemes.length && sa?.technology_type !== 'pump'">
    <h2>小时情景对照</h2>
    <table class="data">
      <thead><tr><th>小时</th><th>节电</th><th>电力相关减排</th><th>节费</th><th>回收期</th></tr></thead>
      <tbody>
        <tr v-for="s in schemes" :key="s.hours">
          <td>{{ s.hours }}</td>
          <td class="num">{{ s.error || fmt(s.annual_electricity_saved_kwh, 0) }}</td>
          <td class="num">{{ fmt(s.scenario_grid_co2_saved_t) }}</td>
          <td class="num">{{ fmt(s.scenario_electricity_cost_saved_cny) }}</td>
          <td>{{ s.simple_payback_years ?? s.payback_message ?? '—' }}</td>
        </tr>
      </tbody>
    </table>
  </div>

  <div class="section">
    <h2>这次用了哪些假设？</h2>
    <ul class="prep-list">
      <li v-for="item in prep" :key="item.key">
        <span class="badge" :class="item.status">{{ item.status === 'ready' ? '已具备' : item.status === 'assumed' ? '采用假设' : item.status === 'needed' ? '可补充' : item.status === 'optional_advanced' ? '可选补充' : item.status }}</span>
        <div><strong>{{ item.label }}</strong><div class="muted">{{ item.detail }}</div></div>
      </li>
    </ul>
  </div>

  <div class="section">
    <h2>来源链接</h2>
    <ul>
      <li v-for="(s, i) in sources" :key="i">
        <span class="tag">{{ s.category }}</span>
        {{ s.name }}
        <a v-if="safeHref(s.url)" :href="safeHref(s.url)" target="_blank" rel="noopener">打开</a>
      </li>
    </ul>
  </div>

  <MethodPanel :mill="sa || mill" />

  <div class="section">
    <h2>报告正文预览</h2>
    <div class="card report-preview" style="max-height:480px;overflow:auto;white-space:pre-wrap;line-height:1.55;background:#fff;color:#111;border:1px solid #DCDCDC">{{ loading ? '加载中…' : (previewError ? previewError : preview) }}</div>
  </div>
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
