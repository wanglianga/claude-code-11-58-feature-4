<template>
  <div>
    <el-card>
      <template #header>
        <div class="bar">
          <span>馈线负荷与容量余量情景(结果锁定同一组拓扑/运行图版本)</span>
          <div>
            <el-button @click="showVersion = !showVersion">
              {{ showVersion ? '收起版本管理' : '版本管理/派生' }}
            </el-button>
            <el-button type="primary" @click="openCreate">新建情景</el-button>
          </div>
        </div>
      </template>
      <el-table :data="rows" size="small" @row-click="(r) => $router.push(`/feeder-scenarios/${r.id}`)">
        <el-table-column prop="code" label="情景编号" width="150" />
        <el-table-column prop="name" label="名称" min-width="200" />
        <el-table-column label="路线" width="130">
          <template #default="{ row }">
            <el-tag :type="METHOD_TAG[row.method]" size="small">{{ METHOD_LABEL[row.method] }}</el-tag>
            <span v-if="row.mc_runs > 1" class="mc">×{{ row.mc_runs }}</span>
          </template>
        </el-table-column>
        <el-table-column label="拓扑/运行图版本" width="200">
          <template #default="{ row }">
            <div>{{ row.assumptions.topology_code }} / {{ row.assumptions.timetable_code }}</div>
          </template>
        </el-table-column>
        <el-table-column label="峰值馈线" width="120">
          <template #default="{ row }">
            <span v-if="row.summary.peak_feeder">
              {{ row.summary.peak_feeder }} {{ row.summary.peak_kw.v }}kW
            </span>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="超限馈线" width="120">
          <template #default="{ row }">
            <el-tag v-for="f in row.summary.feeders_over_capacity" :key="f"
                    type="danger" size="small" style="margin-right:4px">{{ f }}</el-tag>
            <span v-if="!row.summary.feeders_over_capacity.length">-</span>
          </template>
        </el-table-column>
        <el-table-column prop="summary.total_overload_minutes" label="超限分钟" width="90" />
        <el-table-column label="拒纳再生kWh" width="120">
          <template #default="{ row }">{{ row.summary.total_rejected_regen_kwh.v }}</template>
        </el-table-column>
        <el-table-column label="样本稀缺" width="110">
          <template #default="{ row }">
            <el-tag v-for="f in row.summary.scarce_feeders" :key="f"
                    type="warning" size="small" style="margin-right:4px">{{ f }}</el-tag>
            <span v-if="!row.summary.scarce_feeders.length">充足</span>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" width="170" />
      </el-table>
    </el-card>

    <el-card v-if="showVersion" style="margin-top:16px">
      <template #header>
        <div class="bar">
          <span>供电拓扑与运行图生效版本(修订生成新版本,旧版本保留)</span>
          <div>
            <el-button size="small" @click="ttDialog = true">派生运行图</el-button>
            <el-button size="small" @click="topoDialog = true">派生拓扑</el-button>
          </div>
        </div>
      </template>
      <el-row :gutter="16">
        <el-col :span="12">
          <h4>供电拓扑({{ topos.length }})</h4>
          <el-table :data="topos" size="small">
            <el-table-column prop="code" label="版本" width="100" />
            <el-table-column prop="status" label="状态" width="100" />
            <el-table-column label="馈线容量 / 反送边界(kW)">
              <template #default="{ row }">
                <div v-for="f in row.feeders" :key="f.code" class="ver">
                  {{ f.code }}: 容量 {{ f.capacity_kw }} · 受纳边界 {{ f.max_regen_accept_kw }}
                </div>
              </template>
            </el-table-column>
          </el-table>
        </el-col>
        <el-col :span="12">
          <h4>运行图({{ timetables.length }}) · 轨迹库 {{ traces.length }} 条</h4>
          <el-table :data="timetables" size="small">
            <el-table-column prop="code" label="版本" width="150" />
            <el-table-column prop="kind" label="类型" width="100" />
            <el-table-column prop="headway_minutes" label="间隔min" width="80" />
            <el-table-column prop="trip_count" label="班次数" width="70" />
            <el-table-column prop="note" label="说明" min-width="160" />
          </el-table>
        </el-col>
      </el-row>
    </el-card>

    <!-- 新建情景 -->
    <el-dialog v-model="createVisible" title="新建馈线负荷情景" width="560px">
      <el-form label-width="120px">
        <el-form-item label="情景名称"><el-input v-model="form.name" /></el-form-item>
        <el-form-item label="预测路线">
          <el-radio-group v-model="form.method">
            <el-radio value="TRACE_REPLAY">实测轨迹回放</el-radio>
            <el-radio value="ENVELOPE_MC">功率包络联合抽样</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="供电拓扑版本">
          <el-select v-model="form.topology_code" style="width: 100%">
            <el-option v-for="t in topos" :key="t.id" :label="t.code" :value="t.code" />
          </el-select>
        </el-form-item>
        <el-form-item label="运行图版本">
          <el-select v-model="form.timetable_code" style="width: 100%">
            <el-option v-for="t in timetables" :key="t.id" :label="`${t.code}(${t.kind})`"
                       :value="t.code" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="form.method === 'ENVELOPE_MC'" label="联合抽样次数">
          <el-input-number v-model="form.mc_runs" :min="5" :max="200" :step="5" />
        </el-form-item>
        <el-form-item label="基线情景(可选)">
          <el-select v-model="form.baseline_scenario_code" clearable style="width: 100%"
                     placeholder="增量重算时选择已发布基线">
            <el-option v-for="s in rows" :key="s.id" :label="s.code" :value="s.code" />
          </el-select>
        </el-form-item>
        <el-form-item label="改动班次(可选)">
          <el-input v-model="changedText" placeholder="逗号分隔,如 TR-B1,TR-B2;仅这些供电窗口重算" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitCreate">生成情景</el-button>
      </template>
    </el-dialog>

    <!-- 派生运行图 -->
    <el-dialog v-model="ttDialog" title="派生运行图(调整发车时刻)" width="520px">
      <el-form label-width="140px">
        <el-form-item label="新版本编号"><el-input v-model="ttForm.code" /></el-form-item>
        <el-form-item label="源运行图">
          <el-select v-model="ttForm.source_timetable_code" style="width: 100%">
            <el-option v-for="t in timetables" :key="t.id" :label="t.code" :value="t.code" />
          </el-select>
        </el-form-item>
        <el-form-item label="发车间隔(min)"><el-input-number v-model="ttForm.headway_minutes"
                                                              :min="1" :max="60" /></el-form-item>
        <el-form-item label="班次偏移(分钟)">
          <el-input v-model="ttForm.offsetsText" placeholder='JSON,如 {"TR-B1": 3}' />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="ttDialog = false">取消</el-button>
        <el-button type="primary" @click="submitTimetable">派生</el-button>
      </template>
    </el-dialog>

    <!-- 派生拓扑 -->
    <el-dialog v-model="topoDialog" title="派生供电拓扑(修订容量/反送边界)" width="520px">
      <el-form label-width="140px">
        <el-form-item label="新版本编号"><el-input v-model="topoForm.code" /></el-form-item>
        <el-form-item label="源拓扑">
          <el-select v-model="topoForm.source_topology_code" style="width: 100%">
            <el-option v-for="t in topos" :key="t.id" :label="t.code" :value="t.code" />
          </el-select>
        </el-form-item>
        <el-form-item label="馈线覆盖(JSON)">
          <el-input v-model="topoForm.overridesText" type="textarea" :rows="4"
                    placeholder='{"F-S1": {"capacity_kw": 2200, "max_regen_accept_kw": 1500}}' />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="topoDialog = false">取消</el-button>
        <el-button type="primary" @click="submitTopology">派生</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import api from '../api'
import { METHOD_LABEL, METHOD_TAG } from '../utils'

const router = useRouter()
const rows = ref([])
const topos = ref([])
const timetables = ref([])
const traces = ref([])
const showVersion = ref(false)
const createVisible = ref(false)
const ttDialog = ref(false)
const topoDialog = ref(false)
const saving = ref(false)
const changedText = ref('')
const form = ref({ name: '', method: 'TRACE_REPLAY', topology_code: '', timetable_code: '',
                   mc_runs: 40, baseline_scenario_code: null })
const ttForm = ref({ code: '', source_timetable_code: '', headway_minutes: 10, offsetsText: '' })
const topoForm = ref({ code: '', source_topology_code: '', overridesText: '' })

async function load() {
  rows.value = (await api.get('/scenarios')).data
  topos.value = (await api.get('/topologies')).data
  timetables.value = (await api.get('/timetables')).data
  traces.value = (await api.get('/power-traces')).data
}

function openCreate() {
  form.value = {
    name: '', method: 'TRACE_REPLAY',
    topology_code: topos.value[0]?.code || '',
    timetable_code: timetables.value.find((t) => t.kind === 'PROPOSED')?.code
      || timetables.value[0]?.code || '',
    mc_runs: 40, baseline_scenario_code: null,
  }
  changedText.value = ''
  createVisible.value = true
}

async function submitCreate() {
  if (!form.value.name) return ElMessage.warning('请填写情景名称')
  saving.value = true
  try {
    const payload = { ...form.value }
    if (changedText.value.trim()) {
      payload.changed_trip_keys = changedText.value.split(',').map((s) => s.trim()).filter(Boolean)
    }
    const r = await api.post('/scenarios', payload)
    ElMessage.success('情景已生成')
    createVisible.value = false
    await load()
    router.push(`/feeder-scenarios/${r.data.id}`)
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '生成失败')
  } finally {
    saving.value = false
  }
}

async function submitTimetable() {
  try {
    const payload = {
      code: ttForm.value.code, source_timetable_code: ttForm.value.source_timetable_code,
      headway_minutes: ttForm.value.headway_minutes,
      trip_offsets_minutes: ttForm.value.offsetsText ? JSON.parse(ttForm.value.offsetsText) : {},
    }
    await api.post('/timetables/derive', payload)
    ElMessage.success('运行图新版本已派生(基线版本保留)')
    ttDialog.value = false
    await load()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '派生失败,请检查 JSON')
  }
}

async function submitTopology() {
  try {
    const payload = {
      code: topoForm.value.code, source_topology_code: topoForm.value.source_topology_code,
      feeder_overrides: topoForm.value.overridesText ? JSON.parse(topoForm.value.overridesText) : {},
    }
    await api.post('/topologies/derive', payload)
    ElMessage.success('拓扑新版本已派生(旧版本保留)')
    topoDialog.value = false
    await load()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '派生失败,请检查 JSON')
  }
}

onMounted(load)
</script>

<style scoped>
.bar { display: flex; justify-content: space-between; align-items: center; }
.mc { color: #b88230; font-size: 12px; margin-left: 4px; }
.ver { font-size: 12px; color: #555; line-height: 1.7; }
:deep(.el-table__row) { cursor: pointer; }
h4 { margin: 0 0 8px; color: #555; }
</style>
