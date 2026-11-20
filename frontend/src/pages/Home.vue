<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { 批状态, 标 } from '../labels'
const data = ref(null)
const err = ref('')
onMounted(async () => {
  try { data.value = await api.overview() } catch (e) { err.value = e.message }
})
</script>
<template>
  <div>
    <h1>总览</h1>
    <p class="sub">当前账期估抄、实抄、对齐与封印状态</p>
    <p v-if="err" class="msg err">{{ err }}</p>
    <div class="grid" v-if="data">
      <div class="stat">当前账期<b>{{ data.current_period }}</b></div>
      <div class="stat">截止钟口径<b>{{ data.clock }}</b></div>
      <div class="stat">开放账期<b>{{ data.open_periods }}</b></div>
      <div class="stat">已封印账期<b>{{ data.sealed_periods }}</b></div>
      <div class="stat">表具数<b>{{ data.meter_count }}</b></div>
      <div class="stat">未决差额<b>{{ data.pending_variances }}</b></div>
      <div class="stat">批累计未决<b>{{ data.pending_variances }}</b></div>
    </div>
    <p class="sub" v-if="data">发生钟 {{ data.audit_only_clock }} 仅作审计，不参与任何截止/落封</p>
    <div class="panel" v-if="data">
      <h3>最近对账批</h3>
      <table>
        <thead><tr><th>批号</th><th>账期</th><th>钟</th><th>as_of</th><th>匹配</th><th>差额</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="b in data.recent_batches" :key="b.batch_id">
            <td>{{ b.batch_id }}</td>
            <td>{{ b.period_id }}</td>
            <td>{{ b.clock }}</td>
            <td>{{ b.as_of }}</td>
            <td>{{ b.matched_count }}</td>
            <td>{{ b.variance_count }}</td>
            <td><span class="tag" :class="b.status">{{ 标(批状态, b.status) }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
