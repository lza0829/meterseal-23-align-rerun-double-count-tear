<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { 批状态, 命运, 标 } from '../labels'

const periodId = ref('2026-10')
const batch = ref(null)
const vars = ref([])
const snapshot = ref(null)
const msg = ref('')
const err = ref('')

function defaultAsOf() {
  // 截止时刻取当前 UTC，精确到秒；页面只允许回看，不允许未来
  return new Date().toISOString().slice(0, 19)
}
const asOf = ref(defaultAsOf())

async function loadSnapshot(batchId) {
  snapshot.value = await api.snapshots(batchId)
}

async function align() {
  err.value = ''; msg.value = ''
  try {
    // 预览按装载钟 loaded_at 截止；落封必须回传同一 as_of（见封印页）
    batch.value = await api.align(periodId.value, asOf.value)
    vars.value = await api.variances({ batch_id: batch.value.batch_id })
    await loadSnapshot(batch.value.batch_id)
    msg.value = `对齐完成（钟 ${batch.value.clock}，截止 ${batch.value.as_of}）：匹配 ${batch.value.matched_count}，差额 ${batch.value.variance_count}`
  } catch (e) { err.value = e.message }
}
async function fate(id, f) {
  err.value = ''
  try {
    await api.setFate(id, f)
    vars.value = await api.variances({ batch_id: batch.value.batch_id })
  } catch (e) { err.value = e.message }
}
onMounted(async () => {
  const bs = await api.batches(periodId.value)
  if (bs[0]) {
    batch.value = bs[0]
    vars.value = await api.variances({ batch_id: bs[0].batch_id })
    await loadSnapshot(bs[0].batch_id)
  }
})
</script>
<template>
  <div>
    <h1>对齐差额</h1>
    <p class="sub">按表号与账期对齐估抄、实抄 · 全局唯一按装载钟 loaded_at 截止，occurred_at 仅审计</p>
    <div class="row">
      <label>账期<input v-model="periodId" /></label>
      <label>截止时刻 as_of（UTC，≤ 当前）<input v-model="asOf" placeholder="2026-10-31T00:00:00" /></label>
      <button @click="align">跑对齐</button>
    </div>
    <p class="msg ok" v-if="msg">{{ msg }}</p>
    <p class="msg err" v-if="err">{{ err }}</p>
    <div class="panel" v-if="batch">
      <p>
        批号 {{ batch.batch_id }} · 钟口径 <b>{{ batch.clock }}</b> · 截止 as_of <b>{{ batch.as_of }}</b>
        · 指纹 {{ batch.fingerprint }} ·
        <span class="tag" :class="batch.status">{{ 标(批状态, batch.status) }}</span>
      </p>
      <table>
        <thead><tr><th>差额编号</th><th>表号</th><th>估抄</th><th>实抄</th><th>差值</th><th>命运</th><th>处置</th></tr></thead>
        <tbody>
          <tr v-for="v in vars" :key="v.variance_id">
            <td>{{ v.variance_id }}</td>
            <td>{{ v.meter_id }}</td>
            <td>{{ v.estimate_qty }}</td>
            <td>{{ v.actual_qty }}</td>
            <td>{{ v.delta }}</td>
            <td>{{ 标(命运, v.fate) }}</td>
            <td>
              <button class="secondary" @click="fate(v.variance_id, 'accept_short')">认短</button>
              <button class="secondary" @click="fate(v.variance_id, 'accept_over')">认溢</button>
              <button class="warn" @click="fate(v.variance_id, 'quarantine')">隔离</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="panel" v-if="snapshot && snapshot.rows.length">
      <h3>闭包快照（回溯用，钟 {{ snapshot.clock }} / as_of {{ snapshot.as_of }}）</h3>
      <table>
        <thead><tr><th>表号</th><th>估抄</th><th>估抄装载时刻</th><th>实抄</th><th>实抄装载时刻</th><th>发生时刻(审计)</th></tr></thead>
        <tbody>
          <tr v-for="r in snapshot.rows" :key="r.meter_id">
            <td>{{ r.meter_id }}</td>
            <td>{{ r.estimate_qty ?? '—' }}</td>
            <td>{{ r.estimate_loaded_at ?? '—' }}</td>
            <td>{{ r.actual_qty ?? '—' }}</td>
            <td>{{ r.actual_loaded_at ?? '—' }}</td>
            <td>{{ r.actual_occurred_at ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
