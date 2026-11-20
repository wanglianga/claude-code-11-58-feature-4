<template>
  <div v-if="data">
    <el-card>
      <template #header>
        <div class="bar">
          <span>
            {{ data.code }} {{ data.name }}
            <el-tag :type="METHOD_TAG[data.method]" size="small" style="margin-left:6px">
              {{ METHOD_LABEL[data.method] }}
            </el-tag>
            <span v-if="data.mc_runs > 1" class="mc">联合抽样 ×{{ data.mc_runs }}</span>
          </span>
          <span class="meta">
            拓扑 {{ data.assumptions.topology_code }} · 运行图 {{ data.assumptions.timetable_code }}
            · {{ data.range_start.slice(11, 16) }}~{{ data.range_end.slice(11, 16) }}
          </span>
        </div>
      </template>
      <el-descriptions :column="4" border size="small">
        <el-descriptions-item label="峰值馈线">{{ data.summary.peak_feeder }}</el-descriptions-item>
        <el-descriptions-item label="峰值功率">
          <b>{{ data.summary.peak_kw?.v }}</b>
          [{{ data.summary.peak_kw?.lo }} ~ {{ data.summary.peak_kw?.hi }}] kW
        </el-descriptions-item>
        <el-descriptions-item label="总超限分钟">
          <span :class="{ danger: data.summary.total_overload_minutes > 0 }">
            {{ data.summary.total_overload_minutes }}
          </span>
        </el-descriptions-item>
        <el-descriptions-item label="拒纳再生(kWh)">
          {{ data.summary.total_rejected_regen_kwh.v }}
          [{{ data.summary.total_rejected_regen_kwh.lo }} ~ {{ data.summary.total_rejected_regen_kwh.hi }}]
        </el-descriptions-item>
      </el-descriptions>
      <el-alert v-if="data.summary.scarce_feeders.length" type="warning" :closable="false"
                style="margin-top:10px"
                :title="`样本稀缺/无参考轨迹馈线:${data.summary.scarce_feeders.join('、')};预测区间已按匹配层级扩大`" />
    </el-card>

    <el-card style="margin-top:16px">
      <template #header>
        <div class="bar">
          <span>馈线负荷曲线(净负荷=牵引-受纳再生,红线为容量边界)</span>
          <el-select v-model="feederCode" size="small" style="width:160px">
            <el-option v-for="r in data.results" :key="r.feeder_code"
                       :label="r.feeder_code" :value="r.feeder_code" />
          </el-select>
        </div>
      </template>
      <div v-html="chartSvg"></div>
      <div class="legend">
        <span class="lg net">净负荷</span>
        <span class="lg cap">馈线容量</span>
        <span class="lg acc">受纳再生</span>
        <span class="lg rej">无法受纳再生</span>
      </div>
    </el-card>

    <el-card style="margin-top:16px">
      <template #header>各馈线峰值区间、容量余量、超限与再生受纳</template>
      <el-table :data="data.results" size="small" row-key="feeder_code">
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="detail">
              <h4>超限时间窗({{ row.overload_windows.length }})</h4>
              <el-table :data="row.overload_windows" size="small" border>
                <el-table-column prop="start" label="开始" width="170" />
                <el-table-column prop="end" label="结束" width="170" />
                <el-table-column prop="duration_minutes" label="时长min" width="90" />
                <el-table-column label="峰值kW">
                  <template #default="{ row: w }">{{ w.peak_kw.v }} [{{ w.peak_kw.lo }}~{{ w.peak_kw.hi }}]</template>
                </el-table-column>
                <el-table-column label="超额kW" prop="excess_kw" width="90" />
                <el-table-column label="班次组合">
                  <template #default="{ row: w }">
                    <el-tag v-for="t in w.trips" :key="t" size="small" style="margin-right:4px">{{ t }}</el-tag>
                  </template>
                </el-table-column>
              </el-table>
              <h4 style="margin-top:12px">再生无法受纳范围({{ row.rejected_regen.windows.length }})</h4>
              <el-table :data="row.rejected_regen.windows" size="small" border>
                <el-table-column prop="start" label="开始" width="170" />
                <el-table-column prop="end" label="结束" width="170" />
                <el-table-column prop="peak_rejected_kw" label="拒纳峰值kW" width="110" />
                <el-table-column prop="energy_kwh" label="拒纳能量kWh" width="120" />
                <el-table-column label="班次组合">
                  <template #default="{ row: w }">
                    <el-tag v-for="t in w.trips" :key="t" size="small" type="warning"
                            style="margin-right:4px">{{ t }}</el-tag>
                  </template>
                </el-table-column>
              </el-table>
              <h4 style="margin-top:12px">峰值贡献班次</h4>
              <el-table :data="row.contributing_trips" size="small" border>
                <el-table-column prop="trip_key" label="班次" width="120" />
                <el-table-column prop="traction_kw" label="牵引kW" />
                <el-table-column prop="regen_kw" label="再生kW" />
                <el-table-column label="牵引占比">
                  <template #default="{ row: c }">{{ (c.share * 100).toFixed(1) }}%</template>
                </el-table-column>
              </el-table>
              <h4 style="margin-top:12px">轨迹匹配与区间拓宽</h4>
              <el-table :data="row.trace_coverage.matched" size="small" border>
                <el-table-column prop="trip_key" label="班次" width="110" />
                <el-table-column prop="section_code" label="区段" width="80" />
                <el-table-column label="匹配层级" width="170">
                  <template #default="{ row: m }">
                    <el-tag :type="LEVEL_TAG[m.level]" size="small">{{ LEVEL_LABEL[m.level] }}</el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="pool_size" label="样本数" width="80" />
                <el-table-column label="拓宽系数">
                  <template #default="{ row: m }">{{ (m.widen * 100).toFixed(0) }}%</template>
                </el-table-column>
                <el-table-column label="稀缺" width="80">
                  <template #default="{ row: m }">
                    <el-tag v-if="m.scarce" type="warning" size="small">稀缺</el-tag>
                    <span v-else>-</span>
                  </template>
                </el-table-column>
              </el-table>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="feeder_code" label="馈线" width="100" />
        <el-table-column prop="substation_serial" label="牵引所" width="110" />
        <el-table-column label="峰值区间(kW)">
          <template #default="{ row }">
            <b>{{ row.peak.v }}</b> [{{ row.peak.lo }} ~ {{ row.peak.hi }}]
            <div class="ts">@{{ row.peak.at.slice(11, 19) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="容量余量(kW)" width="170">
          <template #default="{ row }">
            <span :class="{ danger: row.margin.v < 0 }">{{ row.margin.v }}</span>
            <div class="ts">[{{ row.margin.lo }} ~ {{ row.margin.hi }}]</div>
          </template>
        </el-table-column>
        <el-table-column label="超限窗" width="90">
          <template #default="{ row }">
            <el-tag :type="row.overload_windows.length ? 'danger' : 'success'" size="small">
              {{ row.overload_windows.length }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="拒纳再生(kWh)" width="150">
          <template #default="{ row }">
            <span :class="{ danger: row.rejected_regen.energy_kwh.v > 0 }">
              {{ row.rejected_regen.energy_kwh.v }}
            </span>
            <div class="ts">[{{ row.rejected_regen.energy_kwh.lo }} ~ {{ row.rejected_regen.energy_kwh.hi }}]</div>
          </template>
        </el-table-column>
        <el-table-column label="受纳边界kW" prop="max_regen_accept_kw" width="100" />
        <el-table-column label="匹配" width="150">
          <template #default="{ row }">
            <el-tag :type="LEVEL_TAG[row.trace_coverage.worst_level]" size="small">
              {{ LEVEL_LABEL[row.trace_coverage.worst_level] }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card style="margin-top:16px">
      <template #header>
        <div class="bar">
          <span>情景版本差异(与另一情景逐馈线对比,基线数据不被替换)</span>
          <el-select v-model="diffId" size="small" clearable placeholder="选择对比情景"
                     style="width:240px" @change="loadDiff">
            <el-option v-for="s in others" :key="s.id" :label="`${s.code} ${s.name}`" :value="s.id" />
          </el-select>
        </div>
      </template>
      <el-table v-if="diff" :data="diff.feeders" size="small">
        <el-table-column prop="feeder_code" label="馈线" width="100" />
        <el-table-column prop="status" label="状态" width="90" />
        <el-table-column label="峰值kW(基线→候选)" width="170">
          <template #default="{ row }">
            {{ row.peak_kw_base ?? '-' }} → <b>{{ row.peak_kw_candidate ?? '-' }}</b>
            <span v-if="row.peak_delta_kw !== undefined" :class="row.peak_delta_kw > 0 ? 'danger' : 'ok'">
              ({{ row.peak_delta_kw > 0 ? '+' : '' }}{{ row.peak_delta_kw }})
            </span>
          </template>
        </el-table-column>
        <el-table-column label="余量kW" width="150">
          <template #default="{ row }">{{ row.margin_base ?? '-' }} → {{ row.margin_candidate ?? '-' }}</template>
        </el-table-column>
        <el-table-column label="超限分钟" width="140">
          <template #default="{ row }">
            {{ row.overload_minutes_base ?? '-' }} → {{ row.overload_minutes_candidate ?? '-' }}
          </template>
        </el-table-column>
        <el-table-column label="拒纳kWh" width="150">
          <template #default="{ row }">
            {{ row.rejected_regen_base_kwh ?? '-' }} → {{ row.rejected_regen_candidate_kwh ?? '-' }}
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else description="选择对比情景查看差异" :image-size="60" />
    </el-card>

    <el-card style="margin-top:16px">
      <template #header>计算假设与增量复算说明</template>
      <el-descriptions :column="3" border size="small">
        <el-descriptions-item label="时间步长">{{ data.assumptions.step_seconds }}s</el-descriptions-item>
        <el-descriptions-item label="抽样次数">{{ data.assumptions.mc_runs }}</el-descriptions-item>
        <el-descriptions-item label="峰值区间阈值">{{ data.assumptions.peak_window_ratio * 100 }}%</el-descriptions-item>
        <el-descriptions-item label="增量模式">
          <el-tag size="small" :type="data.assumptions.incremental.mode === 'INCREMENTAL' ? 'warning' : 'info'">
            {{ data.assumptions.incremental.mode }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="沿用/重算槽位">
          {{ data.assumptions.incremental.carried_slots }} / {{ data.assumptions.incremental.recomputed_slots }}
        </el-descriptions-item>
        <el-descriptions-item label="逐槽不一致">
          <span :class="{ ok: data.assumptions.incremental.mismatches.length === 0,
                           danger: data.assumptions.incremental.mismatches.length > 0 }">
            {{ data.assumptions.incremental.mismatches.length }}
          </span>
        </el-descriptions-item>
      </el-descriptions>
      <el-table v-if="Object.keys(data.assumptions.recomputed_windows).length"
                :data="recomputeRows" size="small" style="margin-top:10px">
        <el-table-column prop="feeder" label="馈线" width="100" />
        <el-table-column label="受影响供电窗口">
          <template #default="{ row }">
            <el-tag v-for="(w, i) in row.windows" :key="i" size="small" style="margin-right:6px">
              {{ w.trip_key }} {{ w.start.slice(11, 16) }}~{{ w.end.slice(11, 16) }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>
      <el-alert v-if="data.assumptions.uncovered_legs.length" type="error" :closable="false"
                style="margin-top:10px" title="存在无馈线电气连接的行程(未计入任何馈线)" />
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'
import { LEVEL_LABEL, LEVEL_TAG, METHOD_LABEL, METHOD_TAG } from '../utils'

const route = useRoute()
const data = ref(null)
const all = ref([])
const diff = ref(null)
const diffId = ref(null)
const feederCode = ref('')

const others = computed(() => all.value.filter((s) => s.id !== Number(route.params.id)))
const recomputeRows = computed(() =>
  Object.entries(data.value.assumptions.recomputed_windows)
    .map(([feeder, windows]) => ({ feeder, windows })))

const current = computed(() =>
  data.value?.results.find((r) => r.feeder_code === feederCode.value) || data.value?.results[0])

const chartSvg = computed(() => {
  const r = current.value
  if (!r || !r.series) return ''
  const W = 960, H = 240, PAD = 36
  const s = r.series
  const n = s.net_v.length
  const maxY = Math.max(r.capacity_kw, ...s.net_hi, ...s.traction_v, ...s.regen_v) * 1.1
  const x = (i) => PAD + (i / Math.max(1, n - 1)) * (W - 2 * PAD)
  const y = (v) => H - PAD - (v / maxY) * (H - 2 * PAD)
  const line = (arr) => arr.map((v, i) => `${x(i)},${y(v)}`).join(' ')
  const capY = y(r.capacity_kw)
  const grid = [0, 0.25, 0.5, 0.75, 1].map((q) => {
    const gy = y(maxY * q)
    return `<line x1="${PAD}" y1="${gy}" x2="${W - PAD}" y2="${gy}" stroke="#eee"/>
      <text x="4" y="${gy + 4}" font-size="10" fill="#999">${Math.round(maxY * q)}</text>`
  }).join('')
  // 再生面积(受纳绿、拒纳橙,绘于零轴下方区域用正功率高度表示)
  const area = (arr, color) => arr.map((v, i) =>
    `<rect x="${x(i) - 1}" y="${H - PAD - (v / maxY) * (H - 2 * PAD)}" width="3"
      height="${(v / maxY) * (H - 2 * PAD)}" fill="${color}" opacity="0.45"/>`).join('')
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" font-family="sans-serif">
    ${grid}
    ${area(s.accepted_v, '#67c23a')}
    ${area(s.rejected_v, '#e6a23c')}
    <polyline fill="none" stroke="#409eff" stroke-width="1.6" points="${line(s.net_v)}"/>
    <polyline fill="none" stroke="#409eff" stroke-width="0.8" stroke-dasharray="4 3"
      opacity="0.6" points="${line(s.net_hi)}"/>
    <polyline fill="none" stroke="#409eff" stroke-width="0.8" stroke-dasharray="4 3"
      opacity="0.6" points="${line(s.net_lo)}"/>
    <line x1="${PAD}" y1="${capY}" x2="${W - PAD}" y2="${capY}" stroke="#f56c6c" stroke-width="1.5"/>
    <text x="${W - PAD - 90}" y="${capY - 5}" font-size="11" fill="#f56c6c">容量 ${r.capacity_kw}kW</text>
  </svg>`
})

async function loadDiff() {
  if (!diffId.value) { diff.value = null; return }
  diff.value = (await api.get(`/scenarios/${route.params.id}/diff?against=${diffId.value}`)).data
}

onMounted(async () => {
  data.value = (await api.get(`/scenarios/${route.params.id}`)).data
  all.value = (await api.get('/scenarios')).data
  feederCode.value = data.value.results[0]?.feeder_code || ''
})
</script>

<style scoped>
.bar { display: flex; justify-content: space-between; align-items: center; }
.meta { color: #888; font-size: 13px; }
.mc { color: #b88230; font-size: 12px; margin-left: 6px; }
.danger { color: #c45656; font-weight: 600; }
.ok { color: #529b2e; }
.ts { color: #999; font-size: 11px; }
.detail { padding: 6px 18px; }
h4 { margin: 8px 0; color: #555; font-size: 13px; }
.legend { margin-top: 6px; font-size: 12px; color: #666; }
.lg { margin-right: 18px; }
.lg.net::before { content: '─'; color: #409eff; font-weight: 700; }
.lg.cap::before { content: '─'; color: #f56c6c; font-weight: 700; }
.lg.acc::before { content: '■'; color: #67c23a; }
.lg.rej::before { content: '■'; color: #e6a23c; }
</style>
