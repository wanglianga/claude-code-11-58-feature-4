<template>
  <div>
    <el-row :gutter="16">
      <el-col :span="6" v-for="c in cards" :key="c.label">
        <el-card shadow="hover">
          <div class="num">{{ c.value }}</div>
          <div class="label">{{ c.label }}</div>
        </el-card>
      </el-col>
    </el-row>
    <el-row :gutter="16" style="margin-top: 16px">
      <el-col :span="12">
        <el-card>
          <template #header>账实核对闭合情况</template>
          <el-progress type="dashboard" :percentage="fullPct" :width="160">
            <span>{{ fullPct }}%</span>
          </el-progress>
          <div class="label" style="margin-top: 8px">
            完全闭合 {{ ov.comparable_full }} / 部分闭合 {{ ov.comparable_partial }} /
            共 {{ ov.reconciliations }} 条核对结论
          </div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <template #header>验收场景速览</template>
          <el-table :data="scenarios" size="small">
            <el-table-column prop="run" label="运行" width="110" />
            <el-table-column prop="scene" label="场景" />
            <el-table-column label="核对" width="90">
              <template #default="{ row }">
                <el-tag :type="COMPARABLE_TAG[row.comparable]" size="small">
                  {{ row.comparable }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import api from '../api'
import { COMPARABLE_TAG } from '../utils'

const ov = ref({})
const runs = ref([])
const scenarios = computed(() => {
  const map = {
    'RUN-1001': '跨区段折返', 'RUN-1002': '设备重启/换编组/倍率切换',
    'RUN-1003': '回馈受限', 'RUN-1004': '车载与供电表差异',
    'RUN-1005': '补传读数(迟到)', 'RUN-1006': '供电表缺失',
  }
  return runs.value
    .filter((r) => map[r.run_key])
    .map((r) => ({ run: `${r.run_key} v${r.version}`, scene: map[r.run_key],
                    comparable: r._comparable || '-' }))
})
const cards = computed(() => [
  { label: '线路 / 区段', value: `${ov.value.lines ?? '-'} / ${ov.value.sections ?? '-'}` },
  { label: '运行(版本)', value: `${ov.value.runs ?? '-'} (${ov.value.run_versions ?? '-'})` },
  { label: '原始读数', value: ov.value.readings ?? '-' },
  { label: '已发布指标', value: ov.value.metrics ?? '-' },
])
const fullPct = computed(() =>
  ov.value.reconciliations
    ? Math.round((ov.value.comparable_full / ov.value.reconciliations) * 100) : 0)

onMounted(async () => {
  ov.value = (await api.get('/meta/overview')).data
  const rs = (await api.get('/runs?all_versions=true')).data
  for (const r of rs) {
    try {
      const rec = (await api.get(`/runs/${r.id}/reconciliation?method=RUN_ALIGNED`)).data
      r._comparable = rec.comparable
    } catch { r._comparable = '-' }
  }
  runs.value = rs
})
</script>

<style scoped>
.num { font-size: 26px; font-weight: 600; color: #1c5d8c; }
.label { color: #888; font-size: 13px; margin-top: 4px; }
</style>
