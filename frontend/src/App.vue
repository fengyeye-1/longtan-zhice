<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, computed } from 'vue'
import { state, refreshAll, applyPayload } from './stores/app'
import { api } from './api/client'
import Overview from './views/Overview.vue'
import Enterprise from './views/Enterprise.vue'
import Retrofit from './views/Retrofit.vue'
import CarbonRoi from './views/CarbonRoi.vue'
import Report from './views/Report.vue'
import Help from './views/Help.vue'

const page = ref('overview')
const appVersion = ref('')
const showWelcome = ref(false)
const menus = [
  { id: 'overview', title: '首页' },
  { id: 'enterprise', title: '企业数据' },
  { id: 'retrofit', title: '设备与改造' },
  { id: 'carbon', title: '投资与报价' },
  { id: 'report', title: '我的评估报告' },
  { id: 'help', title: '使用帮助' },
]

const caseShort = computed(() => {
  const c = state.results?.case
  if (!c) return ''
  if (c.is_demo) return '正在查看：金徽矿业｜公开示例'
  return `正在查看：${c.name}`
})

async function switchEnterprise(id: string) {
  if (!id) return
  try {
    const r = await api.activate(id)
    applyPayload(r)
    await refreshAll()
  } catch (e: any) {
    state.error = e.message || String(e)
  }
}

function onStorage(ev: StorageEvent) {
  if (ev.key === 'ltzs_token' || ev.key === 'ltzs_sid') {
    refreshAll()
  }
}

onMounted(() => {
  refreshAll()
  api.health().then((h) => {
    appVersion.value = [h.version, h.build_id].filter(Boolean).join(' · ')
  }).catch(() => { /* ignore */ })
  try {
    if (localStorage.getItem('ltzs_welcome_dismissed') !== '1') showWelcome.value = true
  } catch { showWelcome.value = true }
  window.addEventListener('storage', onStorage)
})

onBeforeUnmount(() => {
  window.removeEventListener('storage', onStorage)
})

function dismissWelcome(remember = true) {
  showWelcome.value = false
  if (remember) {
    try { localStorage.setItem('ltzs_welcome_dismissed', '1') } catch {}
  }
}

function go(p: string) {
  page.value = p
  if (p === 'help') showWelcome.value = false
}
</script>

<template>
  <header class="topnav">
    <div>
      <div class="brand">陇碳智策</div>
      <div class="sub">省电多少，减碳多少，投入值不值。<span v-if="appVersion"> · {{ appVersion }}</span></div>
    </div>
    <div class="sub case-short">
      <span v-if="state.loading">核算中…</span>
      <span v-else-if="caseShort">{{ caseShort }}</span>
      <label class="field" style="margin:0 12px;display:inline-flex;align-items:center;gap:8px">
        我的企业
        <select :value="state.activeId" @change="switchEnterprise(($event.target as HTMLSelectElement).value)">
          <option v-for="c in state.cases" :key="c.case_id" :value="c.case_id">
            {{ c.name }}{{ c.active ? '（当前）' : '' }}{{ c.is_demo ? ' · 公开示例' : '' }}
          </option>
        </select>
      </label>
      <button class="btn secondary" style="margin-left:12px" type="button" @click="go('help')">使用帮助</button>
    </div>
  </header>

  <div v-if="showWelcome" class="welcome-card card" style="margin:16px 24px 0;max-width:720px">
    <h2 style="margin-top:0">第一次使用？3分钟带你算一项节能改造</h2>
    <p class="muted">可以先看公开案例，再换成自己的设备数据。</p>
    <div class="btn-row">
      <button class="btn" type="button" @click="dismissWelcome(true); go('overview')">跟着示例试一次</button>
      <button class="btn secondary" type="button" @click="dismissWelcome(true)">我已了解，直接进入</button>
      <button class="btn secondary" type="button" @click="go('help'); dismissWelcome(false)">打开使用帮助</button>
    </div>
  </div>

  <div class="layout">
    <aside class="sidebar">
      <button
        v-for="m in menus"
        :key="m.id"
        type="button"
        class="nav-btn"
        :class="{ active: page === m.id }"
        @click="go(m.id)"
      >{{ m.title }}</button>
      <p class="muted" style="margin-top:16px;padding:0 8px">
        可随时切回公开案例。
      </p>
    </aside>
    <main class="main">
      <div v-if="state.error" class="error">{{ state.error }}
        <div class="btn-row"><button class="btn secondary" @click="refreshAll">重试连接</button></div>
      </div>
      <Overview v-if="page === 'overview'" @go="go" />
      <Enterprise v-else-if="page === 'enterprise'" @go="go" />
      <Retrofit v-else-if="page === 'retrofit'" @go="go" />
      <CarbonRoi v-else-if="page === 'carbon'" @go="go" />
      <Report v-else-if="page === 'report'" @go="go" />
      <Help v-else-if="page === 'help'" @go="go" />
    </main>
  </div>
</template>
