<script setup lang="ts">
import { ref } from 'vue'
const emit = defineEmits<{ go: [string] }>()
const tab = ref('quick')
const tabs = [
  { id: 'quick', label: '快速上手' },
  { id: 'upload', label: '自己上传数据' },
  { id: 'quotes', label: '查看供应商报价' },
  { id: 'report', label: '下载评估报告' },
  { id: 'faq', label: '常见问题' },
]
</script>

<template>
  <div class="eyebrow">使用帮助</div>
  <h1>陇碳智策使用说明</h1>
  <p class="lead">想知道设备改造值不值得做？你不需要一次准备好所有资料。可以先看一个公开案例，再把自己的设备信息补进来。平台会告诉你现在能算什么、还需要什么。</p>

  <div class="btn-row" style="flex-wrap:wrap;margin-bottom:16px">
    <button
      v-for="t in tabs"
      :key="t.id"
      type="button"
      class="btn"
      :class="tab === t.id ? '' : 'secondary'"
      @click="tab = t.id"
    >{{ t.label }}</button>
  </div>

  <div v-if="tab === 'quick'" class="card">
    <h2>路线一：先用公开案例试试</h2>
    <ol>
      <li>点击首页的「体验公开案例」。这个案例使用金徽矿业年报披露的历史技改信息，运行小时与投资属于演示假设。</li>
      <li>进入「设备与改造」，选择「选厂球磨机」与「碎磨工艺改进」，先保留 200kW 历史功率变化，再把「每年有效节能时间」改成 2,000、4,000 或 6,000 小时。</li>
      <li>点击「重新计算」，看节电量和相应的电力排放变化。若希望估算节费，去「投资与报价」填写本企业电价，或明确选择「采用金徽参考电价试算」。</li>
      <li>进入「我的评估报告」，选择「一页摘要内容 / 自选章节里的节能减碳」或「完整报告」，确认假设与来源后下载。</li>
    </ol>
    <div class="btn-row">
      <button class="btn" type="button" @click="emit('go', 'overview')">去首页体验公开案例</button>
      <button class="btn secondary" type="button" @click="emit('go', 'retrofit')">去设备与改造</button>
    </div>
  </div>

  <div v-else-if="tab === 'upload'" class="card">
    <h2>路线二：换成自己的企业</h2>
    <ol>
      <li>点击「开始我的企业测算」，填写企业名称和设备所属车间/生产线。</li>
      <li>在「企业数据」选择「年度用电表」「设备信息表」或「改造方案表」，下载模板、填写并上传。页面会先让你确认列名、单位和数据年份。</li>
      <li>在「设备与改造」添加设备或选择已经上传的设备，并填写已知的运行功率、年运行小时或改造前用电量。</li>
      <li>选择改造措施，填写本企业的降功率或节电率、有效节能小时。不能确定的数据可以选择明确标注的参考条件；系统不会把他人案例默认为你的真实数据。</li>
      <li>点击「重新计算」，得到你选中那台设备的节电、相应电力相关减排，以及条件足够时的节费和回本时间。</li>
    </ol>
    <div class="btn-row">
      <button class="btn" type="button" @click="emit('go', 'enterprise')">去企业数据上传</button>
      <button class="btn secondary" type="button" @click="emit('go', 'retrofit')">去添加设备</button>
    </div>
  </div>

  <div v-else-if="tab === 'quotes'" class="card">
    <h2>路线三：查看或选择供应商报价</h2>
    <ol>
      <li>在「投资与报价」选择准备改造的设备，再选择「购买什么/打算做什么」，如变频器采购、水泵采购或泵系统优化。</li>
      <li>平台只展示相关的历史采购项目；点击某个项目可以展开供应商和候选总报价。这里的公司是报价供应商，不是采购企业。</li>
      <li>查看报价年份、数量和规格，核对税费、安装及服务是否包含。资料不足时可以将其收藏为「了解价格水平」，但不能直接当成自己的真实采购价。</li>
      <li>如果已经向供应商询价，可点击「录入我的报价」。若仍要按历史报价做情景实验，勾选「我知道这是参考报价」，系统会在报告中说明。</li>
    </ol>
    <div class="btn-row">
      <button class="btn" type="button" @click="emit('go', 'carbon')">去投资与报价</button>
    </div>
  </div>

  <div v-else-if="tab === 'report'" class="card">
    <h2>路线四：生成报告</h2>
    <p>进入「我的评估报告」，选择完整报告，或者勾选「设备节电」「碳排放变化」「投资与回本」「供应商报价」「方案对比」。如果只想给采购部门看报价，就只选择报价专题；如果只想讨论技术节能，就选择节能减碳。下载前先查看报告预览。即使只导出某一个部分，报告也会保留该部分需要的计算条件和来源。</p>
    <div class="btn-row">
      <button class="btn" type="button" @click="emit('go', 'report')">去我的评估报告</button>
    </div>
  </div>

  <div v-else class="card">
    <h2>常见问题</h2>
    <details open><summary>Q：我没有全年电表数据能用吗？</summary>
      <p>可以先使用公开案例或填写单台设备的信息，能算的部分先算；需要真实电表的结果会提示你补充。</p>
    </details>
    <details><summary>Q：为什么我填了功率，改造前后总电耗还没有？</summary>
      <p>还需要知道该设备过去一年实际运行多久，或者它过去一年的用电量。</p>
    </details>
    <details><summary>Q：为什么已经算出节电，却还没有「节省多少钱」？</summary>
      <p>平台还不知道这家企业每度电实际要付多少钱。可自己填写，也可以明确选用参考价格做情景比较。</p>
    </details>
    <details><summary>Q：报价企业选项在哪里？</summary>
      <p>到「投资与报价」，在「设备采购参考」区域先选设备，再看相应供应商。</p>
    </details>
    <details><summary>Q：参考报价是否等于现在能买到的价格？</summary>
      <p>不是。它来自特定年份的历史采购公告，未必与当前型号、采购数量及服务范围一致。</p>
    </details>
    <details><summary>Q：算出的减碳量能不能直接报给政府？</summary>
      <p>目前只是按指定因子换算的电力相关排放情景；正式核算需要企业实际活动数据、适用标准和复核。</p>
    </details>
    <details><summary>Q：上传的数据会自动变成公开案例吗？</summary>
      <p>不会。公开示范与您的项目应分别管理；公网试用前还需要完成更严格的数据安全措施。</p>
    </details>
    <details><summary>Q：能不能只下载供应商报价报告？</summary>
      <p>可以选择报价专题，报告不会夹带没有选中的其它设备节电结果。</p>
    </details>
  </div>
</template>
