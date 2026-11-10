<script setup lang="ts">
import { computed } from 'vue'
import { state } from '../stores/app'

const props = defineProps<{ mill?: any; defaults?: any }>()

const factorMeta = computed(() => state.results?.factor_meta || state.results?.defaults?.factor_meta)
const sa = computed(() => props.mill || state.results?.selected_assessment || state.results?.assessment_snapshot?.selected_assessment)
const snap = computed(() => state.results?.assessment_snapshot)
const sc = computed(() => state.results?.case?.scenario)
const isDemo = computed(() => !!state.results?.case?.is_demo)
const tech = computed(() => sa.value?.technology_type || sa.value?.source_trace?.mode || 'process')
const sources = computed(() => sa.value?.trace?.param_sources || {})

function srcLine(key: string, fallback: string) {
  const s = sources.value?.[key]
  if (!s) return fallback
  const val = s.value == null ? '未提供' : s.value
  return `${val}${s.unit ? ' ' + s.unit : ''} · ${s.label || s.source_name || s.status || ''}`
}
</script>
<template>
  <details class="method" open>
    <summary>这些数字是怎么算出来的？</summary>
    <div class="body">
      <p v-if="tech === 'pump'">
        当前是水泵类改造：节电量 = 改造前一年用电 × 节电比例。电力相关减排 = 节电量 × 排放换算依据 / 1000。
      </p>
      <p v-else>
        当前是功率优化类改造：节电量 = 功率降幅(kW) × 有效节能小时(h)。电力相关减排 = 节电量 × 排放换算依据 / 1000。
      </p>
      <p>
        <strong>设备 / 措施：</strong>
        {{ sa?.device_name || '—' }} / {{ sa?.measure_name || snap?.selected_measure_name || '—' }}
      </p>
      <p>
        <strong>当前排放换算依据：</strong>
        {{ factorMeta?.label || factorMeta?.source_name || '待确认' }}
        <span v-if="sc?.emission_factor != null">（{{ sc.emission_factor }} kgCO₂/kWh）</span>
      </p>
      <p>
        <strong>当前电价：</strong>
        <template v-if="sa?.source_trace?.tariff?.value != null">
          {{ Number(sa.source_trace.tariff.value).toFixed(4) }} 元/kWh · {{ sa.source_trace.tariff.source || '已选用' }}
        </template>
        <template v-else>
          {{ sc?.energy_price != null ? Number(sc.energy_price).toFixed(4) + ' 元/kWh' : '尚未填写' }}
        </template>
        <span v-if="sa?.tariff_is_reference || sc?.use_jinhui_tariff_reference" class="tag">参考价格、并非本企业实际电价</span>
      </p>
      <p v-if="tech === 'pump'">
        <strong>水泵改造前用电：</strong>{{ sc?.pump_baseline_kwh != null ? sc.pump_baseline_kwh + ' kWh' : '还未填写' }}
      </p>
      <template v-else>
        <p><strong>功率降幅：</strong>{{ srcLine('power_drop_kw', isDemo ? '公开案例披露' : '以当前措施/用户输入为准') }}</p>
        <p><strong>有效节能小时：</strong>{{ srcLine('hours', '情景假设') }}</p>
      </template>
      <p><strong>本措施投资：</strong>{{ (sa?.investment_cny ?? sc?.investment_cny) != null ? Number(sa?.investment_cny ?? sc?.investment_cny).toLocaleString('zh-CN') + ' 元' : '尚未填写' }}</p>
      <p v-if="sa?.formula">计算方法：{{ sa.formula }}</p>
      <p class="muted">说明：节省电费不等于已经减碳；这里只估算与用电相关的排放变化。</p>
      <p v-if="isDemo" class="muted">金徽公开示例中的功率变化与泵节电比例仅作演示，不会自动套到其他企业。</p>
    </div>
  </details>
</template>
