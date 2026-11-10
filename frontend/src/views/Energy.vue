<script setup lang="ts">
import { computed } from 'vue'
import { state } from '../stores/app'
import { safeHref } from '../api/safeHref'

const years = computed(() => state.results?.energy_summary || [])
const maxE = computed(() => Math.max(...years.value.map((y: any) => y.elec_10k_kwh || 0), 1))
</script>

<template>
  <div class="eyebrow">能源档案</div>
  <h1>企业能源活动</h1>
  <p class="lead">企业披露或用户上传的年度数据。公司总量 ≠ 单设备用电。</p>
  <div class="card">
    <h3>工业用电量（万kWh）</h3>
    <div class="chart-bars">
      <div v-for="y in years" :key="y.year" class="bar-wrap">
        <div class="muted">{{ y.elec_10k_kwh }}</div>
        <div class="bar" :style="{ height: (y.elec_10k_kwh / maxE * 120) + 'px' }" />
        <div class="bar-label">{{ y.year }}</div>
      </div>
    </div>
    <p class="muted">灰阶柱状图 · 非实时 · 非伪装小时曲线</p>
  </div>
  <table class="data" style="margin-top:16px">
    <thead>
      <tr>
        <th>年份</th><th>用电万kWh</th><th>电费万元</th><th>强度元/kWh</th><th>类型</th><th>来源</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="y in years" :key="y.year">
        <td>{{ y.year }}</td>
        <td class="num">{{ y.elec_10k_kwh }}</td>
        <td class="num">{{ y.cost_10k_cny }}</td>
        <td class="num">{{ y.intensity_cny_per_kwh }}</td>
        <td>{{ y.data_type }}</td>
        <td><a v-if="safeHref(y.url)" :href="safeHref(y.url)" target="_blank" rel="noopener">打开</a></td>
      </tr>
    </tbody>
  </table>
</template>
