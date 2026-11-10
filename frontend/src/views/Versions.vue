<script setup lang="ts">
import { ref } from 'vue'
import { api } from '../api/client'
import { state, applyPayload, refreshAll } from '../stores/app'

const label = ref('')
const msg = ref('')
const err = ref('')
const cmpA = ref('')
const cmpB = ref('')
const cmp = ref<any>(null)

async function save() {
  err.value = ''
  try {
    const r = await api.saveVersion(label.value || undefined)
    msg.value = `已保存版本 ${r.version_id}`
    await refreshAll()
  } catch (e: any) { err.value = e.message }
}

async function restore(id: string) {
  err.value = ''
  try {
    const r = await api.restoreVersion(id)
    applyPayload(r)
    await refreshAll()
    msg.value = '已恢复版本（若源自示范则写入可编辑副本）'
  } catch (e: any) { err.value = e.message }
}

async function doCompare() {
  err.value = ''
  try {
    cmp.value = await fetch(`/api/versions/compare?a=${encodeURIComponent(cmpA.value)}&b=${encodeURIComponent(cmpB.value)}`).then(async r => {
      if (!r.ok) throw new Error(await r.text())
      return r.json()
    })
  } catch (e: any) { err.value = e.message }
}

async function resetDemo() {
  await api.resetDemo()
  await refreshAll()
  msg.value = '已重置为金徽公开示范'
}
</script>

<template>
  <div class="eyebrow">版本</div>
  <h1>保存 / 恢复 / 比较</h1>
  <p class="lead">用户数据版本可管理；金徽公开示范底账始终可重置恢复。</p>
  <div class="btn-row">
    <input v-model="label" placeholder="版本标签（可选）" style="max-width:220px" />
    <button class="btn" @click="save">保存当前版本</button>
    <button class="btn secondary" @click="resetDemo">重置金徽示范</button>
  </div>
  <div v-if="msg" class="notice">{{ msg }}</div>
  <div v-if="err" class="error">{{ err }}</div>

  <table class="data">
    <thead><tr><th>版本</th><th>标签</th><th>案例</th><th>时间</th><th></th></tr></thead>
    <tbody>
      <tr v-for="v in state.versions" :key="v.version_id">
        <td class="mono">{{ v.version_id }}</td>
        <td>{{ v.label }}</td>
        <td>{{ v.name }}</td>
        <td>{{ v.saved_at }}</td>
        <td><button class="btn secondary" @click="restore(v.version_id)">恢复</button></td>
      </tr>
    </tbody>
  </table>

  <div class="section">
    <h2>版本比较</h2>
    <div class="btn-row">
      <select v-model="cmpA"><option value="">版本 A</option><option v-for="v in state.versions" :key="v.version_id" :value="v.version_id">{{ v.label }}</option></select>
      <select v-model="cmpB"><option value="">版本 B</option><option v-for="v in state.versions" :key="'b'+v.version_id" :value="v.version_id">{{ v.label }}</option></select>
      <button class="btn secondary" @click="doCompare">比较</button>
    </div>
    <pre v-if="cmp" class="mono card">{{ JSON.stringify(cmp, null, 2) }}</pre>
  </div>

  <div class="section">
    <h2>案例列表</h2>
    <table class="data">
      <thead><tr><th>ID</th><th>名称</th><th>示范</th><th>保护</th><th>分级</th><th>当前</th></tr></thead>
      <tbody>
        <tr v-for="c in state.cases" :key="c.case_id">
          <td class="mono">{{ c.case_id }}</td>
          <td>{{ c.name }}</td>
          <td>{{ c.is_demo ? '是' : '否' }}</td>
          <td>{{ c.protected ? '是' : '否' }}</td>
          <td>{{ c.data_grade }}</td>
          <td>{{ c.active ? '●' : '' }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
