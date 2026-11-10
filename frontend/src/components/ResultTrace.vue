<script setup lang="ts">
defineProps<{ trace: any }>()
</script>
<template>
  <details class="method">
    <summary>计算快照（工程师）</summary>
    <div class="body mono">{{ JSON.stringify(trace, null, 2) }}</div>
  </details>
</template>
