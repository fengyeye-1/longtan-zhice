<script setup lang="ts">
import { ref, reactive, computed } from 'vue'
import { state, applyPayload, refreshAll } from '../stores/app'
import { api } from '../api/client'

const kind = ref<'energy' | 'device' | 'retrofit'>('energy')
const file = ref<File | null>(null)
const preview = ref<any>(null)
const mappings = reactive<{ user_column: string; standard_field: string }[]>([])
const msg = ref('')
const err = ref('')
const versionLabel = ref('')
const energyPricePolicy = ref<'keep_manual' | 'use_imported'>('keep_manual')
const mappingError = computed(() => {
  const used = new Map<string, string>()
  for (const m of mappings) {
    const std = (m.standard_field || '').trim()
    if (!std) continue
    if (used.has(std)) {
      return `标准字段「${std}」被重复映射：列「${used.get(std)}」与列「${m.user_column}」`
    }
    used.set(std, m.user_column)
  }
  return ''
})
const currentManualPrice = computed(() => {
  const sc = state.results?.case?.scenario
  if (sc?.energy_price_user_override && sc?.energy_price != null) return Number(sc.energy_price)
  return null
})
const importedAvgPrice = computed(() => {
  if (kind.value !== 'energy' || !preview.value) return null
  const kwhCol = mappings.find(m => m.standard_field === '工业用电量_万kWh')?.user_column
  const costCol = mappings.find(m => m.standard_field === '电力费用_万元')?.user_column
  if (!kwhCol || !costCol) return null
  const rows = preview.value.preview_rows || []
  let kwh = 0, cost = 0
  for (const r of rows) {
    const e = Number(r[kwhCol])
    const c = Number(r[costCol])
    if (Number.isFinite(e) && Number.isFinite(c)) {
      kwh += e
      cost += c
    }
  }
  if (kwh <= 0) return null
  // 万kWh * 10000 = kWh；万元 * 10000 = 元；元/kWh = cost/kwh
  return Math.round((cost / kwh) * 1e6) / 1e6
})
const pricePolicyHint = computed(() => {
  if (kind.value !== 'energy') return ''
  const cur = currentManualPrice.value
  const nxt = importedAvgPrice.value
  if (energyPricePolicy.value === 'keep_manual') {
    if (cur != null) return `将保留当前手动电价 ${cur} 元/kWh，不会改用导入表推算值。`
    return '当前没有手动电价。若表中有电费列，系统只会记录推算值供对照，不会自动改写已有来源。'
  }
  if (nxt != null) {
    return `将采用本次能源表推算的平均电价 ${nxt} 元/kWh` + (cur != null ? `，替换当前手动电价 ${cur} 元/kWh` : '') + '。来源将标记为“导入能源表推算”。'
  }
  return '本次预览未发现可用的电费与用电量列，即使选择“使用本次能源表计算平均电价”，也无法改写电价。'
})

const energy = computed(() => state.results?.energy_summary || [])
const versions = computed(() => state.versions || [])

async function onFile(e: Event) {
  const input = e.target as HTMLInputElement
  file.value = input.files?.[0] || null
  preview.value = null
  msg.value = ''
  err.value = ''
  energyPricePolicy.value = 'keep_manual'
  mappings.splice(0)
  if (!file.value) return
  try {
    const p = await api.previewUpload(kind.value, file.value)
    preview.value = p
    for (const s of p.suggested_mappings || []) mappings.push({ ...s })
    for (const c of p.columns || []) {
      if (!mappings.find(m => m.user_column === c)) {
        mappings.push({ user_column: c, standard_field: '' })
      }
    }
  } catch (ex: any) {
    err.value = ex.message
  }
}

async function commit() {
  if (!file.value) return
  err.value = ''
  msg.value = ''
  if (mappingError.value) {
    err.value = mappingError.value
    return
  }
  try {
    const map = mappings.filter(m => m.standard_field)
    const r = await api.commitUpload(kind.value, file.value, map, {
      energyPricePolicy: kind.value === 'energy' ? energyPricePolicy.value : 'keep_manual',
    })
    applyPayload(r)
    if (r.access_token) {
      // token 已由 client 写入
    }
    await refreshAll()
    msg.value = r.message || '导入成功并已重算'
    if (r.coverage_note) msg.value += '；' + r.coverage_note
  } catch (ex: any) {
    err.value = ex.message
  }
}

function energySourceTag(y: any) {
  const dt = String(y?.data_type || '')
  const note = String(y?.note || '')
  const copied = Boolean(state.results?.case?.origin_case_id === 'jinhui-demo')
  if (dt === '披露' || note.includes('公司合并披露')) {
    return state.results?.case?.is_demo ? '公开资料' : (copied ? '示范复制·公开资料' : '公开资料')
  }
  if (dt.includes('用户提供') || dt.includes('未核验')) return '用户上传'
  if (state.results?.case?.is_demo) return '公开资料'
  return dt || '用户上传'
}

async function restoreDemo() {
  const r = await api.resetDemo()
  applyPayload(r)
  await refreshAll()
  msg.value = '已恢复金徽公开案例（仅本会话）'
}

async function createBlank() {
  err.value = ''
  try {
    const r = await api.createEmptyEnterprise('新企业项目')
    applyPayload(r)
    await refreshAll()
    msg.value = r.message || '已创建空白企业，未复制金徽示范数据'
  } catch (ex: any) {
    err.value = ex.message
  }
}

async function enableReference() {
  try {
    const r = await api.updateScenario({ use_jinhui_reference: true })
    applyPayload(r)
    await refreshAll()
    msg.value = '已显式启用金徽参考情景'
  } catch (ex: any) {
    err.value = ex.message
  }
}

async function saveVer() {
  await api.saveVersion(versionLabel.value || undefined)
  await refreshAll()
  msg.value = '版本已保存'
}

async function restoreVer(id: string) {
  const r = await api.restoreVersion(id)
  applyPayload(r)
  await refreshAll()
}

async function switchEnterprise(id: string) {
  if (!id) return
  try {
    const r = await api.activate(id)
    applyPayload(r)
    await refreshAll()
    msg.value = `已切换到「${state.results?.case?.name || id}」`
  } catch (ex: any) {
    err.value = ex.message
  }
}
</script>

<template>
  <div class="eyebrow">企业数据</div>
  <h1>先把企业的用电情况放进来</h1>
  <p class="lead">有Excel就直接上传。要一份完全空白的企业请点「新建空白企业」；「恢复公开案例」会加载金徽示范，复制后再改会保留公开历史并标明来源。</p>

  <div class="card" style="margin-bottom:16px">
    <label class="field">我的企业 / 切换
      <select :value="state.activeId" @change="switchEnterprise(($event.target as HTMLSelectElement).value)">
        <option v-for="c in state.cases" :key="c.case_id" :value="c.case_id">
          {{ c.name }}{{ c.active ? '（当前）' : '' }}{{ c.is_demo ? ' · 公开示例' : '' }}
        </option>
      </select>
    </label>
    <p class="muted">切换后同步当前项目凭证、设备、措施和报告，不会串用其他企业的数据。</p>
  </div>

  <div class="btn-row">
    <button class="btn" @click="restoreDemo">恢复公开案例</button>
    <button class="btn secondary" @click="createBlank">新建空白企业</button>
    <button class="btn secondary" @click="enableReference">启用金徽参考情景</button>
    <a class="btn secondary" :href="api.templateUrl('energy')">下载年度用电表</a>
    <a class="btn secondary" :href="api.templateUrl('device')">下载设备信息表</a>
    <a class="btn secondary" :href="api.templateUrl('retrofit')">下载改造方案表</a>
  </div>

  <div class="section">
    <h2>企业这几年用了多少电？</h2>
    <p class="muted">公司全年用电量；单台设备数据需要另外填写，不会自动分摊。</p>
    <table class="data" v-if="energy.length">
      <thead>
        <tr><th>年份</th><th>用电 万kWh</th><th>电费 万元</th><th>平均每度电费用（元）</th><th>标记</th></tr>
      </thead>
      <tbody>
        <tr v-for="y in energy" :key="y.year">
          <td>{{ y.year }}</td>
          <td class="num">{{ y.elec_10k_kwh }}</td>
          <td class="num">{{ y.cost_10k_cny == null ? '待填写' : y.cost_10k_cny }}</td>
          <td class="num">{{ y.intensity_cny_per_kwh == null ? '—' : y.intensity_cny_per_kwh }}</td>
          <td><span class="tag">{{ energySourceTag(y) }}</span></td>
        </tr>
      </tbody>
    </table>
  </div>

  <div class="section">
    <h2>上传数据</h2>
    <div class="card">
      <label class="field">这次要上传哪类资料？
        <select v-model="kind">
          <option value="energy">企业能源活动</option>
          <option value="device">设备参数</option>
          <option value="retrofit">技术改造项目</option>
        </select>
      </label>
      <label class="field">选择文件（≤5MB / ≤1万行）
        <input type="file" accept=".csv,.xlsx" @change="onFile" />
      </label>
      <p class="muted">请确认单位：万kWh↔kWh、万元↔元。系统后台统一用 kWh / 元。</p>
    </div>

    <div v-if="preview" class="section" style="border:none;margin-top:12px;padding-top:0">
      <h3>预览前 20 行 · 共 {{ preview.row_count }} 行</h3>
      <p class="notice">{{ preview.will_replace }}</p>
      <table class="data" v-if="preview.preview_rows?.length">
        <thead>
          <tr>
            <th v-for="c in preview.columns" :key="c">{{ c }}
              <div class="muted" v-if="preview.missing_rate">缺 {{ Math.round((preview.missing_rate[c] || 0)*100) }}%</div>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in preview.preview_rows" :key="i">
            <td v-for="c in preview.columns" :key="c">{{ row[c] == null || row[c] === '' ? '（空）' : row[c] }}</td>
          </tr>
        </tbody>
      </table>
      <h3 style="margin-top:12px">核对一下表格列名</h3>
      <table class="data">
        <thead>
          <tr><th>用户列</th><th>映射到标准字段</th></tr>
        </thead>
        <tbody>
          <tr v-for="m in mappings" :key="m.user_column">
            <td>{{ m.user_column }}</td>
            <td>
              <select v-model="m.standard_field">
                <option value="">（跳过）</option>
                <option v-for="f in (preview.standard_fields || [])" :key="f" :value="f">{{ f }}</option>
              </select>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="mappingError" class="error">{{ mappingError }}</div>
      <div v-if="kind==='energy'" class="card" style="margin:12px 0">
        <p class="muted">导入能源表时请明确电价策略。默认保留您已经手动设定的电价，不会静默覆盖。</p>
        <label class="field"><input type="radio" value="keep_manual" v-model="energyPricePolicy" /> 保留我手动设定的电价（推荐）</label>
        <label class="field"><input type="radio" value="use_imported" v-model="energyPricePolicy" /> 使用本次能源表计算平均电价</label>
        <p class="notice">{{ pricePolicyHint }}</p>
        <p class="muted" v-if="currentManualPrice != null">当前手动电价：{{ currentManualPrice }} 元/kWh</p>
        <p class="muted" v-if="importedAvgPrice != null">本次表推算平均电价（预览行）：{{ importedAvgPrice }} 元/kWh</p>
        <p class="muted" v-else-if="kind==='energy'">本次预览尚未映射电费列，或没有可用的电费数据。</p>
      </div>
      <div class="btn-row">
        <button class="btn" :disabled="!!mappingError" @click="commit">保存数据，更新测算</button>
      </div>
    </div>
    <div v-if="msg" class="notice">{{ msg }}</div>
    <div v-if="err" class="error">{{ err }}</div>
  </div>

  <div class="section">
    <h2>我保存过的测算</h2>
    <div class="btn-row">
      <input style="max-width:220px" v-model="versionLabel" placeholder="版本备注（可选）" />
      <button class="btn secondary" @click="saveVer">保存当前版本</button>
    </div>
    <table class="data" v-if="versions.length">
      <thead><tr><th>标签</th><th>案例</th><th>时间</th><th></th></tr></thead>
      <tbody>
        <tr v-for="v in versions" :key="v.version_id">
          <td>{{ v.label }}</td>
          <td>{{ v.case_id }}</td>
          <td>{{ v.saved_at }}</td>
          <td><button class="btn secondary" @click="restoreVer(v.version_id)">恢复</button></td>
        </tr>
      </tbody>
    </table>
    <p class="muted" v-else>暂无已保存版本。</p>
  </div>
</template>
