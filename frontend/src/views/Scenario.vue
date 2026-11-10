<script setup lang="ts">
import { reactive, ref, watch, computed } from 'vue'
import { state, applyPayload, refreshAll } from '../stores/app'
import { api } from '../api/client'

const form = reactive({
  hours: 4000,
  power_drop_kw: 200,
  energy_price: 0.4548,
  emission_factor: 0.4471,
  investment_cny: 1000000,
  annual_maintenance_cny: 0,
  efficiency: null as number | null,
  output_t: null as number | null,
})

watch(() => state.results?.case?.scenario, (sc) => {
  if (!sc) return
  Object.assign(form, {
    hours: sc.hours,
    power_drop_kw: sc.power_drop_kw,
    energy_price: sc.energy_price,
    emission_factor: sc.emission_factor,
    investment_cny: sc.investment_cny,
    annual_maintenance_cny: sc.annual_maintenance_cny,
    efficiency: sc.efficiency,
    output_t: sc.output_t,
  })
}, { immediate: true })

const err = ref('')
const custom = reactive({
  name: '', unit: '', source: '', target: '', value: 0, mapped_model_field: '' as string,
})

async function apply() {
  err.value = ''
  try {
    const body: Record<string, number> = {
      hours: form.hours,
      power_drop_kw: form.power_drop_kw,
      energy_price: form.energy_price,
      emission_factor: form.emission_factor,
      investment_cny: form.investment_cny,
      annual_maintenance_cny: form.annual_maintenance_cny,
    }
    if (form.efficiency != null) body.efficiency = form.efficiency
    if (form.output_t != null) body.output_t = form.output_t
    const r = await api.updateScenario(body)
    applyPayload(r)
    await refreshAll()
  } catch (e: any) {
    err.value = e.message
  }
}

function preset(h: number) {
  form.hours = h
  apply()
}

async function addCustom() {
  err.value = ''
  try {
    const body: any = { ...custom }
    if (!body.mapped_model_field) body.mapped_model_field = null
    const r = await api.addCustomParam(body)
    applyPayload(r)
    await refreshAll()
  } catch (e: any) {
    err.value = e.message
  }
}

const mill = computed(() => state.results?.mill_scenario)
</script>

<template>
  <div class="eyebrow">参数</div>
  <h1>参数自定义与情景</h1>
  <p class="lead">修改运行小时等参数后重算减排与经济收益。自定义参数须填写名称/单位/来源/适用对象，并映射到已验证模型字段后才参与计算。</p>

  <div class="twocol">
    <div class="card">
      <span class="tag">假设</span>
      <label class="field">年有效节能小时：{{ form.hours }}
        <input type="range" min="0" max="8000" step="50" v-model.number="form.hours" @change="apply" />
      </label>
      <div class="btn-row">
        <button class="btn secondary" @click="preset(2000)">2000h</button>
        <button class="btn secondary" @click="preset(4000)">4000h</button>
        <button class="btn secondary" @click="preset(6000)">6000h</button>
      </div>
      <label class="field">功率降低 kW<input type="number" v-model.number="form.power_drop_kw" /></label>
      <label class="field">能源价格 元/kWh<input type="number" step="0.0001" v-model.number="form.energy_price" /></label>
      <label class="field">排放因子 kgCO₂/kWh<input type="number" step="0.0001" v-model.number="form.emission_factor" /></label>
      <label class="field">投资 元<input type="number" v-model.number="form.investment_cny" /></label>
      <label class="field">年维护费 元<input type="number" v-model.number="form.annual_maintenance_cny" /></label>
      <label class="field">设备效率（可选）<input type="number" step="0.01" v-model.number="form.efficiency" /></label>
      <label class="field">产量 t（可选）<input type="number" v-model.number="form.output_t" /></label>
      <button class="btn" @click="apply">应用并重算</button>
      <div v-if="err" class="error">{{ err }}</div>
    </div>
    <div class="card">
      <span class="tag">模型估算</span>
      <h3>当前情景结果</h3>
      <div v-if="mill">
        <div class="label">节电</div><div class="metric">{{ mill.annual_electricity_saved_kwh?.toLocaleString('zh-CN') }}</div>
        <div class="label" style="margin-top:10px">减排 tCO₂</div><div class="metric">{{ mill.scenario_grid_co2_saved_t }}</div>
        <div class="label" style="margin-top:10px">电费节省 元</div><div class="metric">{{ mill.scenario_electricity_cost_saved_cny }}</div>
        <p class="muted">is_actual_annual_savings = {{ mill.is_actual_annual_savings }}</p>
      </div>
    </div>
  </div>

  <div class="section">
    <h2>自定义参数（映射后才入算）</h2>
    <div class="card">
      <div class="twocol">
        <label class="field">名称<input v-model="custom.name" /></label>
        <label class="field">单位<input v-model="custom.unit" /></label>
        <label class="field">数据来源<input v-model="custom.source" /></label>
        <label class="field">适用对象<input v-model="custom.target" /></label>
        <label class="field">数值<input type="number" v-model.number="custom.value" /></label>
        <label class="field">映射模型字段
          <select v-model="custom.mapped_model_field">
            <option value="">不参与计算</option>
            <option value="hours">hours</option>
            <option value="power_drop_kw">power_drop_kw</option>
            <option value="energy_price">energy_price</option>
            <option value="emission_factor">emission_factor</option>
            <option value="investment_cny">investment_cny</option>
            <option value="annual_maintenance_cny">annual_maintenance_cny</option>
          </select>
        </label>
      </div>
      <button class="btn secondary" @click="addCustom">添加自定义参数</button>
      <table class="data" style="margin-top:12px" v-if="state.results?.case?.custom_params?.length">
        <thead><tr><th>名称</th><th>值</th><th>单位</th><th>来源</th><th>对象</th><th>映射</th></tr></thead>
        <tbody>
          <tr v-for="(p,i) in state.results.case.custom_params" :key="i">
            <td>{{ p.name }}</td><td>{{ p.value }}</td><td>{{ p.unit }}</td>
            <td>{{ p.source }}</td><td>{{ p.target }}</td><td>{{ p.mapped_model_field || '未映射' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
