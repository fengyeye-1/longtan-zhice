<script setup lang="ts">
import { computed } from 'vue'
import { state, applyPayload, refreshAll } from '../stores/app'
import { api } from '../api/client'
import MethodPanel from '../components/MethodPanel.vue'

const emit = defineEmits<{ go: [string] }>()

const sa = computed(() => state.results?.selected_assessment || state.results?.assessment_snapshot?.selected_assessment)
const mill = computed(() => state.results?.selected_assessment || state.results?.mill_scenario)
const schemes = computed(() => state.results?.schemes || [])
const isDemo = computed(() => !!state.results?.case?.is_demo)
const retrofits = computed(() => state.results?.case?.retrofits || [])
const devices = computed(() => state.results?.case?.devices || [])

function fmt(n: number | null | undefined, digits = 0) {
  if (n == null || Number.isNaN(n)) return null
  return Number(n).toLocaleString('zh-CN', { maximumFractionDigits: digits })
}

function schemeCell(s: any, value: number | null | undefined, digits = 0) {
  if (s?.inapplicable_reason || s?.status === 'invalid_input') {
    return s.inapplicable_reason || s.error || s.payback_message || '超过该设备可用运行小时，不适用'
  }
  return fmt(value, digits) ?? '—'
}

function wanDu(n: number | null | undefined) {
  if (n == null) return null
  if (n >= 10000) return `${fmt(n / 10000, 1)} 万度`
  return `${fmt(n)} 度`
}

async function useDemo() {
  const r = await api.resetDemo()
  applyPayload(r)
  await refreshAll()
}

async function selectJinhui() {
  if (!confirm('切回金徽公开案例？不会覆盖你已保存在本会话中的企业项目。')) return
  const r = await api.activate('jinhui-demo')
  applyPayload(r)
  await refreshAll()
}

function paybackText() {
  const m = sa.value
  if (!m) return '—'
  if (m.simple_payback_years != null) return `约 ${m.simple_payback_years} 年`
  return m.payback_message || '这项改造要花多少钱还没确定，先不计算回本时间。'
}

function costText() {
  const m = sa.value
  if (!m) return '—'
  if (m.annual_cost_saving_cny != null) {
    const base = fmt(m.annual_cost_saving_cny, 2) + ' 元'
    return m.tariff_is_reference ? base + '（参考电价）' : base
  }
  return m.cost_note || '节电和减碳已经算出来了。补充每度电的费用后，还能看看一年省多少钱。'
}
</script>

<template>
  <div class="eyebrow">首页</div>
  <h1>从一台设备开始，算清节电、减碳和回本。</h1>
  <p class="lead">选一台设备，算清它能省多少电、少排多少碳，再决定要不要改。先试试甘肃企业的公开案例，也可以上传自己的设备数据。</p>

  <div class="btn-row">
    <button class="btn" @click="useDemo">体验公开案例</button>
    <button class="btn secondary" @click="emit('go', 'enterprise')">开始我的企业测算</button>
    <button class="btn secondary" @click="emit('go', 'help')">使用帮助</button>
    <button class="btn secondary" @click="selectJinhui">切回金徽公开案例</button>
  </div>

  <p class="muted">免费在线实例（如 SnapDeploy、Render）休眠或重新部署后，容器里的企业数据可能丢失。公开示范写在应用代码中，可点「体验公开案例」恢复；私有数据请用本机一键启动保存，并导出 TXT/CSV/HTML。不要把企业数据提交到公开代码仓库。</p>

  <div class="notice" v-if="state.results">
    <strong>{{ isDemo ? '正在查看：金徽矿业｜公开示例' : ('正在查看：' + state.results.case?.name) }}</strong>
    <span class="tag" style="margin-left:8px">{{ isDemo ? '公开资料' : '用户项目' }}</span>
    <span class="tag" v-if="sa?.measure_name">措施：{{ sa.measure_name }}</span>
    <span class="tag" v-if="sa?.device_name">设备：{{ sa.device_name }}</span>
  </div>

  <div class="section" style="border-top:none;margin-top:0;padding-top:0">
    <h2>这项改造，一年可能带来什么变化？</h2>
    <p class="muted" v-if="sa?.technology_type !== 'pump' && sa?.saved_kwh">
      这里先按历史功率变化与假设节能小时计算，并不是这台设备的实测全年用电。
    </p>
    <p class="muted" v-else-if="sa?.waiting_message">{{ sa.waiting_message }}</p>
  </div>

  <div v-if="sa?.saved_kwh != null" class="cardrow cardrow-4">
    <div class="card">
      <span class="tag tag-abs">当前措施</span>
      <div class="label">{{ sa.display_energy_label || '预计一年省电' }}</div>
      <div class="metric">{{ wanDu(sa.energy_direction === 'increase' ? (sa.increased_kwh ?? Math.abs(sa.saved_kwh)) : sa.saved_kwh) }}</div>
      <div class="unit">{{ fmt(sa.saved_kwh) }} kWh · {{ sa.device_name || '' }} / {{ sa.measure_name || '' }}</div>
    </div>
    <div class="card">
      <span class="tag tag-abs">情景估算</span>
      <div class="label">{{ sa.display_carbon_label || '预计减少的二氧化碳排放' }}</div>
      <div class="metric">{{ fmt(sa.energy_direction === 'increase' ? Math.abs(sa.co2_reduction_t) : sa.co2_reduction_t, 2) }}</div>
      <div class="unit">tCO₂ · 按所选电力排放因子（注明年份）估算，不是核证减排量</div>
    </div>
    <div class="card">
      <span class="tag tag-abs">情景估算</span>
      <div class="label">预计一年少花多少电费</div>
      <div class="metric" :class="{ pending: sa.annual_cost_saving_cny == null }">{{ costText() }}</div>
      <div class="unit">{{ sa.annual_cost_saving_cny == null ? '待补电价' : '按当前电价估算' }}</div>
      <button v-if="sa.annual_cost_saving_cny == null" class="btn secondary" type="button" style="margin-top:8px" @click="emit('go', 'carbon')">去填写电价</button>
    </div>
    <div class="card">
      <span class="tag tag-abs">情景估算</span>
      <div class="label">这笔改造大约多久回本？</div>
      <div class="metric" :class="{ pending: sa.simple_payback_years == null }">{{ paybackText() }}</div>
      <div class="unit">需本措施投资与电价</div>
      <button v-if="sa.simple_payback_years == null" class="btn secondary" type="button" style="margin-top:8px" @click="emit('go', 'carbon')">去补充投资或电价</button>
    </div>
  </div>
  <div v-else-if="sa?.waiting_message" class="notice">{{ sa.waiting_message }}
    <div class="btn-row" style="margin-top:8px">
      <button class="btn secondary" @click="emit('go', 'retrofit')">去补充数据</button>
    </div>
  </div>
  <div v-else-if="state.results?.mill_error && !sa" class="error">{{ state.results.mill_error }}</div>

  <p class="muted">测算链条：设备节电 → 电力相关减排情景 → 成本收益。排放因子注明年份；情景估算不等于核证减排。</p>
  <p class="warn" v-if="sa?.safety_unverified">可以先查看节能潜力，但通风安全条件尚未确认，暂不提供实施推荐。</p>
  <p class="warn" v-if="(sa?.consistency_warnings || []).length">{{ sa.consistency_warnings[0] }}</p>
  <div class="section">
    <h2>你想先看哪台设备？</h2>
    <div class="tech-cards" v-if="isDemo">
      <div class="card">
        <h3>选厂球磨机</h3>
        <p class="muted">碎磨工艺改进 · 历史披露功率降低 200 kW。参考条件，不代表其他企业已经实现。</p>
        <button class="btn secondary" @click="emit('go', 'retrofit')">去测算</button>
      </div>
      <div class="card">
        <h3>二级输送泵</h3>
        <p class="muted">二级输送泵改进 · 披露电耗下降 12.93%。需填写该泵改造前一年用电后再计算。</p>
        <button class="btn secondary" @click="emit('go', 'retrofit')">去测算</button>
      </div>
    </div>
    <div class="tech-cards" v-else-if="retrofits.length || devices.length">
      <div class="card" v-for="r in retrofits" :key="r.id">
        <h3>{{ r.target_device || r.name }}</h3>
        <p class="muted">{{ r.name }}</p>
        <button class="btn secondary" @click="emit('go', 'retrofit')">去测算</button>
      </div>
    </div>
    <div class="card" v-else>
      <h3>还没有设备或改造措施</h3>
      <p class="muted">添加一台设备和一项改造，或先体验公开案例。</p>
      <button class="btn" @click="emit('go', 'retrofit')">去添加设备</button>
    </div>
  </div>

  <div class="section">
    <button class="btn secondary" type="button" @click="emit('go', 'retrofit')">让结果更贴近实际 → 补充数据</button>
  </div>

  <div class="section" v-if="schemes.length && sa?.technology_type !== 'pump'">
    <h2>每年省电的时间不同，结果差多少？</h2>
        <div class="table-scroll">
      <table class="data">
      <thead>
        <tr><th>有效小时（假设）</th><th>节电 kWh</th><th>电力相关减排 tCO₂</th><th>节费 元</th></tr>
      </thead>
      <tbody>
        <tr v-for="s in schemes" :key="s.hours">
          <td>{{ s.hours }}</td>
          <td class="num">{{ schemeCell(s, s.annual_electricity_saved_kwh) }}</td>
          <td class="num">{{ schemeCell(s, s.scenario_grid_co2_saved_t, 2) }}</td>
          <td class="num">{{ schemeCell(s, s.scenario_electricity_cost_saved_cny, 2) }}</td>
        </tr>
      </tbody>
    </table>
      <p class="muted table-scroll-hint">宽表可左右滑动查看，避免文字被压成单字换行</p>
    </div>
  </div>

  <MethodPanel :mill="sa || mill" :defaults="state.results?.defaults" />
</template>
