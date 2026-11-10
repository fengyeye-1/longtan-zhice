<script setup lang="ts">
import { ref, reactive, computed } from 'vue'
import { api } from '../api/client'
import { applyPayload, refreshAll } from '../stores/app'

const kind = ref<'energy' | 'device' | 'retrofit'>('energy')
const file = ref<File | null>(null)
const preview = ref<any>(null)
const mappings = reactive<{ user_column: string; standard_field: string }[]>([])
const msg = ref('')
const err = ref('')
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

async function onFile(e: Event) {
  const input = e.target as HTMLInputElement
  file.value = input.files?.[0] || null
  preview.value = null
  msg.value = ''
  err.value = ''
  mappings.splice(0)
  if (!file.value) return
  try {
    const p = await api.previewUpload(kind.value, file.value)
    preview.value = p
    for (const s of p.suggested_mappings || []) mappings.push({ ...s })
    // ensure each column has a row
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
    await refreshAll()
    msg.value = r.message || '导入成功并已重算'
    if (r.warnings && r.warnings.length) msg.value += '。' + r.warnings.join('；')
  } catch (ex: any) {
    err.value = ex.message
  }
}
</script>

<template>
  <div class="eyebrow">导入</div>
  <h1>动态数据导入</h1>
  <p class="lead">支持 CSV / XLSX。预览 → 字段映射 → 校验。无效文件拒绝；金徽示范不会被永久覆盖。</p>

  <div class="btn-row">
    <a class="btn secondary" :href="api.templateUrl('energy')">下载能源模板</a>
    <a class="btn secondary" :href="api.templateUrl('device')">下载设备模板</a>
    <a class="btn secondary" :href="api.templateUrl('retrofit')">下载技改模板</a>
  </div>

  <div class="card">
    <label class="field">模板类型
      <select v-model="kind">
        <option value="energy">企业能源活动</option>
        <option value="device">设备参数</option>
        <option value="retrofit">技术改造项目</option>
      </select>
    </label>
    <label class="field">选择文件
      <input type="file" accept=".csv,.xlsx" @change="onFile" />
    </label>
  </div>

  <div v-if="preview" class="section">
    <h2>预览（前 20 行）· 共 {{ preview.row_count }} 行</h2>
    <table class="data">
      <thead>
        <tr><th v-for="c in preview.columns" :key="c">{{ c }}</th></tr>
      </thead>
      <tbody>
        <tr v-for="(row, i) in preview.preview_rows" :key="i">
          <td v-for="c in preview.columns" :key="c">{{ row[c] }}</td>
        </tr>
      </tbody>
    </table>

    <h2 style="margin-top:16px">字段映射</h2>
    <table class="data">
      <thead><tr><th>用户列</th><th>标准字段</th></tr></thead>
      <tbody>
        <tr v-for="m in mappings" :key="m.user_column">
          <td>{{ m.user_column }}</td>
          <td>
            <select v-model="m.standard_field">
              <option value="">（不映射）</option>
              <option v-if="kind==='energy'" v-for="f in ['年份','工业用电量_万kWh','电力费用_万元','柴油使用量_万升','柴油费用_万元','边界说明','来源URL']" :key="f" :value="f">{{ f }}</option>
              <option v-if="kind==='device'" v-for="f in ['设备名称','设备类型','功率_kW','年运行小时','效率','基准年电耗_kWh','产量_t','数据来源','数据类型']" :key="f" :value="f">{{ f }}</option>
              <option v-if="kind==='retrofit'" v-for="f in ['项目名称','目标设备','功率降低_kW','节电比例','投资_元','年维护费_元','数据来源','备注']" :key="f" :value="f">{{ f }}</option>
            </select>
          </td>
        </tr>
      </tbody>
    </table>
    <div v-if="kind==='energy'" class="card" style="margin:12px 0">
      <p class="muted">若本企业已有手动电价，导入时默认保留手动价，不会静默覆盖。冲突时会在结果中记录新旧值与来源。</p>
      <label class="field"><input type="radio" value="keep_manual" v-model="energyPricePolicy" /> 保留手动电价（推荐）</label>
      <label class="field"><input type="radio" value="use_imported" v-model="energyPricePolicy" /> 改用导入表推算电价</label>
    </div>
    <div v-if="mappingError" class="error">{{ mappingError }}</div>
    <div class="btn-row">
      <button class="btn" :disabled="!!mappingError" @click="commit">校验并导入（自动重算）</button>
    </div>
  </div>
  <div v-if="msg" class="notice">{{ msg }}</div>
  <div v-if="err" class="error">{{ err }}</div>
</template>
