<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { 账期状态, 批状态, 标 } from '../labels'
const periods = ref([])
const periodId = ref('2026-10')
const batches = ref([])
const batchId = ref('')
const msg = ref('')
const err = ref('')

const current = () => batches.value.find(b => b.batch_id === batchId.value)

async function refresh() {
  periods.value = await api.periods()
  batches.value = await api.batches(periodId.value)
  batchId.value = batches.value.find(b => b.status === 'open')?.batch_id || batches.value[0]?.batch_id || ''
}
async function seal() {
  err.value = ''; msg.value = ''
  try {
    const b = current()
    // 落封回传批次固化的同一只钟、同一个 as_of；后端会再校验一次，不一致即 409
    const r = await api.seal({
      period_id: periodId.value,
      batch_id: batchId.value || null,
      clock: b?.clock,
      as_of: b?.as_of || null,
    })
    msg.value = `已封印 ${r.period_id}，绑定批 ${r.batch_id}（钟 ${r.clock}，截止 ${r.as_of}）`
    await refresh()
  } catch (e) { err.value = e.message }
}
onMounted(refresh)
</script>
<template>
  <div>
    <h1>账期封印</h1>
    <p class="sub">封印后该期估抄、实抄禁止再写 · 落封与对齐预览必须同一只钟、同一 as_of</p>
    <div class="row">
      <label>账期<input v-model="periodId" @change="refresh" /></label>
      <label>对账批<input v-model="batchId" placeholder="可空取最新开放批" /></label>
      <button class="warn" @click="seal">封印账期</button>
      <button class="secondary" @click="refresh">刷新</button>
    </div>
    <p class="panel" v-if="current()">
      本批钟口径 <b>{{ current().clock }}</b> · 固化 as_of <b>{{ current().as_of }}</b>
      · 状态 <span class="tag" :class="current().status">{{ 标(批状态, current().status) }}</span>
    </p>
    <p class="msg ok" v-if="msg">{{ msg }}</p>
    <p class="msg err" v-if="err">{{ err }}</p>
    <div class="panel">
      <table>
        <thead><tr><th>账期</th><th>状态</th><th>封印时刻</th><th>封印钟</th><th>封印 as_of</th><th>绑定批</th></tr></thead>
        <tbody>
          <tr v-for="p in periods" :key="p.period_id">
            <td>{{ p.period_id }}</td>
            <td><span class="tag" :class="p.status">{{ 标(账期状态, p.status) }}</span></td>
            <td>{{ p.sealed_at || '—' }}</td>
            <td>{{ p.sealed_clock || '—' }}</td>
            <td>{{ p.sealed_as_of || '—' }}</td>
            <td>{{ p.batch_no || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
