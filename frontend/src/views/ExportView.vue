<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from '../api/client'
import { state } from '../stores/app'
import { safeHref } from '../api/safeHref'

const report = ref('')

onMounted(async () => {
  try {
    report.value = await fetch(api.exportReportUrl).then(r => r.text())
  } catch (e: any) {
    report.value = e.message
  }
})
</script>

<template>
  <div class="eyebrow">导出</div>
  <h1>报告与来源</h1>
  <p class="lead">下载带来源与假设说明的报告。结果区分披露 / 文献 / 模型估算 / 假设。</p>
  <div class="btn-row">
    <a class="btn" :href="api.exportReportUrl" download="陇碳智策_报告.txt">下载文本报告</a>
    <a class="btn secondary" :href="api.exportCsvUrl" download="陇碳智策_结果.csv">下载 CSV</a>
  </div>

  <div class="section">
    <h2>来源清单</h2>
    <table class="data">
      <thead><tr><th>名称</th><th>类别</th><th>URL</th></tr></thead>
      <tbody>
        <tr v-for="(s,i) in state.results?.case?.sources || []" :key="i">
          <td>{{ s.name }}</td>
          <td>{{ s.category }}</td>
          <td><a v-if="safeHref(s.url)" :href="safeHref(s.url)" target="_blank" rel="noopener">打开</a></td>
        </tr>
      </tbody>
    </table>
  </div>

  <div class="section">
    <h2>报告预览</h2>
    <pre class="mono card">{{ report }}</pre>
  </div>
</template>
