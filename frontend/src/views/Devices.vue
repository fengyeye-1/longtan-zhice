<script setup lang="ts">
import { reactive, ref, computed, onMounted } from 'vue'
import { state, applyPayload, refreshAll } from '../stores/app'
import { api } from '../api/client'

const form = reactive({
  name: '新增大功率风机',
  device_type: '矿井通风机',
  power_kw: 80 as number | null,
  hours: 3000 as number | null,
  baseline_kwh: null as number | null,
  source: '页面新增',
})
const msg = ref('')
const err = ref('')
const equipmentOptions = ref<any[]>([])

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

async function add() {
  err.value = ''
  msg.value = ''
  try {
    const r = await api.addDevice({ ...form, data_type: '假设' })
    applyPayload(r)
    await refreshAll()
    msg.value = '设备已保存。继续补充运行数据后，再计算改造效果。'
  } catch (e: any) {
    err.value = e.message
  }
}

onMounted(async () => {
  try {
    const data = await api.equipmentTypes()
    equipmentOptions.value = data.types || []
  } catch {
    equipmentOptions.value = []
  }
})
</script>

<template>
  <div class="eyebrow">设备</div>
  <h1>设备清单与页面新增</h1>
  <p class="lead">先选设备，我们会显示适合它的测算方法。新增设备后可在「设备与改造」页创建措施。</p>

  <div class="twocol">
    <div class="card">
      <h3>新增设备</h3>
      <label class="field">名称<input v-model="form.name" /></label>
      <label class="field">类型
        <select v-model="form.device_type">
          <option v-for="opt in equipmentOptions" :key="opt.type_code" :value="opt.display_name">
            {{ opt.display_name }}
          </option>
          <option v-if="!equipmentOptions.length" value="矿井通风机">矿井通风机</option>
        </select>
      </label>
      <label class="field">功率 kW<input type="number" v-model.number="form.power_kw" min="0" /></label>
      <label class="field">年运行小时<input type="number" v-model.number="form.hours" min="0" max="8784" /></label>
      <label class="field">基准年电耗 kWh（可选）
        <input type="number" v-model.number="form.baseline_kwh" min="0" />
      </label>
      <button class="btn" type="button" @click="add">新增并重算</button>
      <div v-if="msg" class="notice">{{ msg }}</div>
      <div v-if="err" class="error">{{ err }}</div>
    </div>
    <div class="card">
      <span class="tag">模型估算</span>
      <h3>企业设备情景能耗合计</h3>
      <div class="metric">{{ state.results?.enterprise_devices?.scenario_total_kwh?.toLocaleString('zh-CN') ?? '—' }}</div>
      <div class="unit">kWh · {{ state.results?.enterprise_devices?.formula }}</div>
      <p class="muted">{{ state.results?.enterprise_devices?.boundary }}</p>
    </div>
  </div>

  <div class="section">
    <h2>当前设备</h2>
    <div class="device-card-list">
      <div class="device-card" v-for="d in devices" :key="d.id">
        <div class="device-card-title">{{ d.name }}</div>
        <div class="device-card-meta">
          <span>{{ d.device_type }}</span>
          <span>{{ d.power_kw == null ? '功率待填' : d.power_kw + ' kW' }}</span>
          <span>{{ d.hours == null ? '小时待填' : d.hours + ' h' }}</span>
          <span>{{ d.baseline_kwh == null ? '基准 —' : d.baseline_kwh + ' kWh' }}</span>
          <span>{{ d.data_type }}</span>
          <span class="mono">{{ d.id }}</span>
          <button class="btn secondary" type="button" @click="copyDeviceId(d.id)">{{ copiedId === d.id ? '已复制ID' : '复制设备ID' }}</button>
        </div>
      </div>
    </div>
    <div class="table-scroll">
      <table class="data device-table-wide">
        <thead>
          <tr><th>名称</th><th>类型</th><th>设备ID</th><th>功率kW</th><th>小时</th><th>基准kWh</th><th>类型标签</th><th>来源</th></tr>
        </thead>
        <tbody>
          <tr v-for="d in devices" :key="'w-' + d.id">
            <td>{{ d.name }}</td>
            <td>{{ d.device_type }}</td>
            <td class="mono">{{ d.id }} <button class="btn secondary" type="button" @click="copyDeviceId(d.id)">{{ copiedId === d.id ? '已复制' : '复制' }}</button></td>
            <td class="num">{{ d.power_kw }}</td>
            <td class="num">{{ d.hours }}</td>
            <td class="num">{{ d.baseline_kwh ?? '—' }}</td>
            <td>{{ d.data_type }}</td>
            <td>{{ d.source }}</td>
          </tr>
        </tbody>
      </table>
      <p class="muted table-scroll-hint">宽表可左右滑动查看</p>
    </div>
  </div>
</template>

<style scoped>
.device-card-list { display: none; gap: 10px; margin-bottom: 12px; }
.device-card { border: 1px solid #E5E5E5; border-radius: 10px; padding: 12px; background: #fff; }
.device-card-title { font-weight: 600; margin-bottom: 6px; word-break: normal; overflow-wrap: break-word; }
.device-card-meta { display: flex; flex-wrap: wrap; gap: 8px 12px; color: #555; font-size: 13px; }
.device-card-meta > span { white-space: nowrap; }
.table-scroll { overflow-x: auto; }
.table-scroll-hint { display: none; font-size: 12px; }
@media (max-width: 768px) {
  .device-card-list { display: grid; }
  .device-table-wide { display: none; }
  .table-scroll-hint { display: block; }
}
@media (max-width: 390px) {
  .device-card { overflow: hidden; }
  .device-card-meta { font-size: 12px; }
}
</style>
