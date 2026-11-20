<template>
  <section class="page" data-module="cache-warmup">
    <header class="page-head">
      <div>
        <h2>缓存重建批次包</h2>
        <p class="page-desc">
          一次为多个缓存目标按真实 OLAP 查询结果批量预热；条目携带数据版本与可命中参数范围，
          联合查询只在同一版本内对账，禁止跨版本混算。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="loadAll">刷新数据</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <nav class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab-btn"
        :class="{ active: activeTab === tab.key }"
        type="button"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </nav>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <!-- 共享输入数据集 -->
    <div v-if="activeTab === 'dataset'" class="panel">
      <h3>共享输入数据集 {{ meta.dataset }}（多查询共用）</h3>
      <p class="page-desc">
        同一份事实表的两个数据版本：v1=更新前（总计 100），v2=更新后（总计 120）。
        OLAP 引擎按显式版本取数，不会跨版本读取。
      </p>
      <div v-for="version in meta.versions" :key="version" class="version-block">
        <h4>数据版本 {{ version }}</h4>
        <table class="data-table">
          <thead>
            <tr><th>区域</th><th>品类</th><th>销售额</th></tr>
          </thead>
          <tbody>
            <tr v-for="(row, idx) in meta.facts[version]" :key="idx">
              <td>{{ row.region }}</td>
              <td>{{ row.category }}</td>
              <td>{{ row.amount }}</td>
            </tr>
          </tbody>
          <tfoot>
            <tr><td>合计</td><td>—</td><td>{{ versionTotals[version] }}</td></tr>
          </tfoot>
        </table>
      </div>
    </div>

    <!-- 缓存目标与分组 -->
    <div v-if="activeTab === 'targets'" class="panel">
      <h3>缓存目标（登记查询、固定参数与容量）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>目标编号</th><th>名称</th><th>查询类型</th><th>固定参数</th>
            <th>容量</th><th>TTL(秒)</th><th>状态</th><th>当前版本</th><th>容量调整</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in targets" :key="t.target_id">
            <td>{{ t.target_id }}</td>
            <td>{{ t.name }}</td>
            <td>{{ t.query_kind === 'total' ? '总计' : '分组' }}</td>
            <td class="mono">{{ formatFixedParams(t.fixed_params) }}</td>
            <td>{{ t.max_capacity }}</td>
            <td>{{ t.ttl_seconds }}</td>
            <td>{{ t.status }}</td>
            <td>{{ t.current_version ?? '—' }}</td>
            <td class="row-actions">
              <input v-model.number="capacityDraft[t.target_id]" class="capacity-input" type="number" min="1" />
              <button class="link" type="button" @click="updateCapacity(t)">应用</button>
            </td>
          </tr>
        </tbody>
      </table>

      <h3 style="margin-top:18px">同组任务（缓存入口多选、整组提交）</h3>
      <div v-for="g in groups" :key="g.group_name" class="group-block">
        <h4>{{ g.group_name }}</h4>
        <label v-for="t in g.targets" :key="t.target_id" class="check-item">
          <input v-model="groupSelection[g.group_name]" :value="t.target_id" type="checkbox" />
          <span>{{ t.name }}（{{ t.target_id }}，容量 {{ t.max_capacity }}）</span>
        </label>
        <div class="row-actions" style="margin-top:8px">
          <select v-model="groupVersion[g.group_name]">
            <option v-for="v in meta.versions" :key="v" :value="v">{{ v }}</option>
          </select>
          <button class="btn primary" type="button" @click="submitGroup(g.group_name)">一次提交整组</button>
        </div>
        <table v-if="groupResults[g.group_name]" class="data-table" style="margin-top:10px">
          <thead>
            <tr><th>目标</th><th>是否纳入</th><th>状态</th><th>已生成版本</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr v-for="p in groupResults[g.group_name].per_target" :key="p.target_id">
              <td>{{ p.name }}</td>
              <td>{{ p.included ? '已纳入' : '未纳入' }}</td>
              <td :class="p.status === '已完成' ? 'ok-text' : 'warn-text'">{{ p.status }}</td>
              <td>{{ p.generated_version ?? '—' }}</td>
              <td>{{ p.reason ?? p.detail ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 重建批次包 -->
    <div v-if="activeTab === 'batches'" class="panel">
      <h3>生成缓存重建批次包（多目标、真实查询结果）</h3>
      <div class="filter-bar">
        <label class="modal-field">
          <span>数据版本（固定参数）</span>
          <select v-model="batchForm.data_version">
            <option v-for="v in meta.versions" :key="v" :value="v">{{ v }}</option>
          </select>
        </label>
        <label class="modal-field">
          <span>TTL 秒（可选，覆盖目标默认值）</span>
          <input v-model.number="batchForm.ttl_seconds" type="number" min="1" placeholder="留空用目标默认 TTL" />
        </label>
      </div>
      <label v-for="t in targets" :key="t.target_id" class="check-item">
        <input v-model="batchForm.target_ids" :value="t.target_id" type="checkbox" />
        <span>{{ t.name }}（{{ t.query_kind === 'total' ? '总计' : '分组' }}，容量 {{ t.max_capacity }}）</span>
      </label>
      <div style="margin-top:10px">
        <button class="btn primary" type="button" @click="createBatch">按真实查询生成批次包</button>
      </div>

      <h3 style="margin-top:18px">历史批次包</h3>
      <table class="data-table">
        <thead>
          <tr><th>批次编号</th><th>数据版本</th><th>状态</th><th>成功目标</th><th>失败目标</th><th>逐项结果</th></tr>
        </thead>
        <tbody>
          <tr v-for="b in batches" :key="b.batch_id">
            <td class="mono">{{ b.batch_id }}</td>
            <td>{{ b.data_version }}</td>
            <td :class="b.status === '已完成' ? 'ok-text' : 'warn-text'">{{ b.status }}</td>
            <td>{{ b.succeeded_targets.join('、') || '—' }}</td>
            <td>{{ b.failed_targets.join('、') || '—' }}</td>
            <td>
              <div v-for="i in b.items" :key="i.target_id" class="item-line">
                <strong>{{ i.name }}</strong>：{{ i.status }}
                <span v-if="i.reason" class="warn-text">（{{ i.reason }}）</span>
                <span v-else>，生成 {{ i.records.length }} 条条目</span>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 缓存条目与命中 -->
    <div v-if="activeTab === 'records'" class="panel">
      <h3>缓存条目（可追溯数据版本与有效期）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>条目编号</th><th>目标</th><th>版本</th><th>查询</th><th>参数</th>
            <th>可命中范围</th><th>实际结果</th><th>来源批次</th><th>有效期</th><th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in records" :key="r.record_id">
            <td class="mono">{{ r.record_id }}</td>
            <td>{{ r.target_name }}</td>
            <td>{{ r.data_version }}</td>
            <td>{{ r.query_kind === 'total' ? '总计' : '分组' }}</td>
            <td class="mono">{{ JSON.stringify(r.params) }}</td>
            <td class="mono">{{ formatRange(r.param_range) }}</td>
            <td class="mono">{{ formatResult(r.result) }}</td>
            <td class="mono">{{ r.batch_id }}</td>
            <td>{{ formatExpiry(r) }}</td>
            <td :class="r.status === '有效' ? 'ok-text' : 'warn-text'">{{ r.status }}</td>
          </tr>
        </tbody>
      </table>

      <h3 style="margin-top:18px">按参数试命中（参数范围 + 版本 + 有效期三道闸门）</h3>
      <div class="filter-bar">
        <label class="modal-field">
          <span>缓存目标</span>
          <select v-model="lookupForm.target_id">
            <option v-for="t in targets" :key="t.target_id" :value="t.target_id">{{ t.name }}</option>
          </select>
        </label>
        <label class="modal-field">
          <span>region 参数</span>
          <input v-model="lookupForm.region" placeholder="如 华东 / ALL / 华南" />
        </label>
        <label class="modal-field">
          <span>限定数据版本（可选）</span>
          <select v-model="lookupForm.data_version">
            <option :value="null">不限定</option>
            <option v-for="v in meta.versions" :key="v" :value="v">{{ v }}</option>
          </select>
        </label>
        <button class="btn primary" type="button" @click="doLookup">试命中</button>
      </div>
      <div v-if="lookupResult" class="lookup-box">
        <p :class="lookupResult.hit ? 'ok-text' : 'warn-text'">
          {{ lookupResult.hit ? `命中 ${lookupResult.record.record_id}（版本 ${lookupResult.record.data_version}）` : `未命中：${lookupResult.reason}` }}
        </p>
        <table v-if="lookupResult.hit" class="data-table">
          <tbody>
            <tr><td>实际结果</td><td class="mono">{{ formatResult(lookupResult.record.result) }}</td></tr>
            <tr><td>有效期至</td><td>{{ lookupResult.record.expires_at }}</td></tr>
          </tbody>
        </table>
        <p class="page-desc">逐条核对：</p>
        <ul class="check-list">
          <li v-for="c in lookupResult.checked" :key="c.record_id" :class="c.hit ? 'ok-text' : 'warn-text'">
            {{ c.record_id }} / {{ c.data_version }} — {{ c.hit ? '满足' : c.reason }}
          </li>
        </ul>
      </div>
    </div>

    <!-- 联合查询对账 -->
    <div v-if="activeTab === 'reconcile'" class="panel">
      <h3>总计 + 分组联合查询对账（同版本对回，禁止混算）</h3>
      <div class="filter-bar">
        <label class="modal-field">
          <span>总计目标</span>
          <select v-model="reconcileForm.total_target_id">
            <option v-for="t in totalTargets" :key="t.target_id" :value="t.target_id">{{ t.name }}</option>
          </select>
        </label>
        <label class="modal-field">
          <span>分组目标</span>
          <select v-model="reconcileForm.group_target_id">
            <option v-for="t in groupTargets" :key="t.target_id" :value="t.target_id">{{ t.name }}</option>
          </select>
        </label>
        <label class="modal-field">
          <span>口径策略</span>
          <select v-model="reconcileForm.policy">
            <option v-for="p in metaPolicies" :key="p.value" :value="p.value">{{ p.label }}</option>
          </select>
        </label>
        <button class="btn primary" type="button" @click="doReconcile">联合对账</button>
        <button class="btn" type="button" @click="doMixedGuard">查看跨版本拦截</button>
      </div>

      <div v-if="reconcileReport" class="report-box">
        <p v-if="!reconcileReport.ok" class="warn-text">对账不可用：{{ reconcileReport.reason }}</p>
        <template v-else>
          <p class="ok-text">{{ reconcileReport.declaration }}</p>
          <table class="data-table">
            <tbody>
              <tr><td>服务版本</td><td>{{ reconcileReport.served_version }}</td></tr>
              <tr><td>来源说明</td><td>{{ reconcileReport.sourced_from }}</td></tr>
              <tr><td>总计</td><td>{{ reconcileReport.total }}</td></tr>
              <tr>
                <td>分组求和</td>
                <td>{{ reconcileReport.group_sum }}（{{ reconcileReport.reconciled ? '与总计同版对回 ✓' : '对不上 ✗' }}）</td>
              </tr>
              <tr>
                <td>分组明细</td>
                <td class="mono">{{ reconcileReport.groups.map((g: GroupRow) => `${g.region}=${g.amount}`).join('，') }}</td>
              </tr>
            </tbody>
          </table>
        </template>
      </div>

      <div v-if="mixedGuard" class="report-box">
        <h4>跨版本混算拦截（{{ mixedGuard.rule }}）</h4>
        <table v-if="mixedGuard.blocked_attempts.length" class="data-table">
          <thead><tr><th>总计版本</th><th>分组版本</th><th>拦截</th><th>原因</th></tr></thead>
          <tbody>
            <tr v-for="(a, idx) in mixedGuard.blocked_attempts" :key="idx">
              <td>{{ a.total_version }}（{{ a.would_total }}）</td>
              <td>{{ a.group_version }}（{{ a.would_group_sum }}）</td>
              <td class="warn-text">{{ a.blocked ? '已拦截' : '未拦截' }}</td>
              <td>{{ a.reason }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="page-desc">当前没有跨版本组合可演示。</p>
      </div>
    </div>

    <footer class="page-foot">
      <span>缓存重建批次包 · 共享数据集 {{ meta.dataset ?? 'shared_sales' }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

const ENDPOINT = '/api/cache-warmup'

interface Target {
  target_id: string
  name: string
  query_kind: string
  fixed_params: Array<Record<string, unknown>>
  ttl_seconds: number
  max_capacity: number
  group_name: string | null
  status: string
  current_version: string | null
}
interface GroupRow { region: string; amount: number }
interface CacheRecord {
  record_id: string
  target_name: string
  data_version: string
  query_kind: string
  params: Record<string, unknown>
  param_range: Array<Record<string, unknown>>
  result: Record<string, any>
  batch_id: string
  ttl_seconds: number
  created_at: number
  expires_at: number
  status: string
}

const tabs = [
  { key: 'dataset', label: '共享数据集' },
  { key: 'targets', label: '目标与分组' },
  { key: 'batches', label: '重建批次包' },
  { key: 'records', label: '条目与命中' },
  { key: 'reconcile', label: '联合对账' },
]
const activeTab = ref('dataset')
const errorMessage = ref('')

const meta = reactive<{ dataset: string; versions: string[]; facts: Record<string, any[]>; policies: Array<{ value: string; label: string }> }>({
  dataset: 'shared_sales', versions: ['v1', 'v2'], facts: {}, policies: [],
})
const targets = ref<Target[]>([])
const groups = ref<Array<{ group_name: string; target_ids: string[]; targets: Target[] }>>([])
const batches = ref<any[]>([])
const records = ref<CacheRecord[]>([])
const overview = ref<Record<string, number>>({})

const capacityDraft = reactive<Record<string, number>>({})
const groupSelection = reactive<Record<string, string[]>>({})
const groupVersion = reactive<Record<string, string>>({})
const groupResults = reactive<Record<string, any>>({})

const batchForm = reactive<{ target_ids: string[]; data_version: string; ttl_seconds: number | null }>({
  target_ids: [], data_version: 'v1', ttl_seconds: null,
})
const lookupForm = reactive<{ target_id: string; region: string; data_version: string | null }>({
  target_id: '', region: '', data_version: null,
})
const lookupResult = ref<any>(null)
const reconcileForm = reactive({ total_target_id: '', group_target_id: '', policy: 'valid_combo' })
const reconcileReport = ref<any>(null)
const mixedGuard = ref<any>(null)

const totalTargets = computed(() => targets.value.filter((t) => t.query_kind === 'total'))
const groupTargets = computed(() => targets.value.filter((t) => t.query_kind === 'group'))
const metaPolicies = computed(() => meta.policies)
const stats = computed(() => [
  { label: '缓存目标', value: overview.value.targets ?? targets.value.length },
  { label: '重建批次包', value: overview.value.batches ?? batches.value.length },
  { label: '有效条目', value: overview.value.valid_records ?? 0 },
  { label: '过期条目', value: overview.value.expired_records ?? 0 },
  { label: '失败批次', value: overview.value.failed_batches ?? 0 },
])
const versionTotals = computed<Record<string, number>>(() => {
  const out: Record<string, number> = {}
  for (const [version, rows] of Object.entries(meta.facts)) {
    out[version] = rows.reduce((sum: number, r: any) => sum + Number(r.amount), 0)
  }
  return out
})

async function getJson(path: string) {
  const resp = await request(path)
  if (!resp.ok) throw new Error(`接口返回 ${resp.status}`)
  return resp.json()
}

async function postJson(path: string, body: unknown) {
  const resp = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(data.detail || data.message || `接口返回 ${resp.status}`)
  return data
}

function formatFixedParams(params: Array<Record<string, unknown>>) {
  if (!params.length) return '（无固定参数）'
  return params.map((p) => (p.values ? `${p.name} ∈ [${(p.values as unknown[]).join('/')}]` : `${p.name} ∈ ${JSON.stringify(p.range)}`)).join('；')
}
function formatRange(range: Array<Record<string, unknown>>) {
  if (!range.length) return '（空范围，仅无参查询可命中）'
  return range.map((p) => (p.op === 'eq' ? `${p.name}=${p.value}` : p.op === 'in' ? `${p.name}∈${JSON.stringify(p.values)}` : `${p.name}∈[${p.low},${p.high}]`)).join('；')
}
function formatResult(result: Record<string, any>) {
  if (result.query_kind === 'total') return `total=${result.total}`
  const groups = (result.groups || []).map((g: GroupRow) => `${g.region}:${g.amount}`).join(',')
  return `total=${result.total}; {${groups}}`
}
function formatExpiry(r: CacheRecord) {
  return `${r.created_at} → ${r.expires_at}（${r.ttl_seconds}s）`
}

async function loadAll() {
  errorMessage.value = ''
  try {
    const [metaData, targetData, groupData, batchData, recordData, overviewData] = await Promise.all([
      getJson(`${ENDPOINT}/meta`),
      getJson(`${ENDPOINT}/targets`),
      getJson(`${ENDPOINT}/groups`),
      getJson(`${ENDPOINT}/batches`),
      getJson(`${ENDPOINT}/records`),
      getJson(`${ENDPOINT}/overview`),
    ])
    Object.assign(meta, metaData)
    targets.value = targetData.items
    groups.value = groupData.items
    batches.value = batchData.items
    records.value = recordData.items
    overview.value = overviewData
    for (const t of targets.value) {
      if (capacityDraft[t.target_id] === undefined) capacityDraft[t.target_id] = t.max_capacity
    }
    for (const g of groups.value) {
      if (!groupSelection[g.group_name]) groupSelection[g.group_name] = []
      if (!groupVersion[g.group_name]) groupVersion[g.group_name] = meta.versions[0]
    }
    if (!lookupForm.target_id && targets.value.length) lookupForm.target_id = targets.value[0].target_id
    if (!reconcileForm.total_target_id && totalTargets.value.length) reconcileForm.total_target_id = totalTargets.value[0].target_id
    if (!reconcileForm.group_target_id && groupTargets.value.length) reconcileForm.group_target_id = groupTargets.value[0].target_id
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据加载失败'
  }
}

async function updateCapacity(t: Target) {
  errorMessage.value = ''
  try {
    await postJson(`${ENDPOINT}/targets/${t.target_id}/capacity`, { max_capacity: capacityDraft[t.target_id] })
    await loadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '容量调整失败'
  }
}

async function submitGroup(groupName: string) {
  errorMessage.value = ''
  try {
    const result = await postJson(`${ENDPOINT}/groups/${encodeURIComponent(groupName)}/submit`, {
      target_ids: groupSelection[groupName],
      data_version: groupVersion[groupName],
    })
    groupResults[groupName] = result
    await loadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '整组提交失败'
  }
}

async function createBatch() {
  errorMessage.value = ''
  try {
    if (!batchForm.target_ids.length) throw new Error('请至少勾选一个缓存目标')
    const payload: Record<string, unknown> = { target_ids: batchForm.target_ids, data_version: batchForm.data_version }
    if (batchForm.ttl_seconds) payload.ttl_seconds = batchForm.ttl_seconds
    await postJson(`${ENDPOINT}/batches`, payload)
    await loadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批次生成失败'
  }
}

async function doLookup() {
  errorMessage.value = ''
  lookupResult.value = null
  try {
    const params: Record<string, string> = {}
    if (lookupForm.region.trim()) params.region = lookupForm.region.trim()
    const body: Record<string, unknown> = { target_id: lookupForm.target_id, params }
    if (lookupForm.data_version) body.data_version = lookupForm.data_version
    lookupResult.value = await postJson(`${ENDPOINT}/lookup`, body)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '命中查询失败'
  }
}

async function doReconcile() {
  errorMessage.value = ''
  reconcileReport.value = null
  try {
    reconcileReport.value = await postJson(`${ENDPOINT}/reconcile`, { ...reconcileForm })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '对账失败'
  }
}

async function doMixedGuard() {
  errorMessage.value = ''
  mixedGuard.value = null
  try {
    const qs = `total_target_id=${encodeURIComponent(reconcileForm.total_target_id)}&group_target_id=${encodeURIComponent(reconcileForm.group_target_id)}`
    mixedGuard.value = await getJson(`${ENDPOINT}/mixed-guard?${qs}`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '拦截查询失败'
  }
}

onMounted(loadAll)
</script>

<style scoped>
.tab-bar { display: flex; gap: 6px; margin-bottom: 14px; flex-wrap: wrap; }
.tab-btn { border: 1px solid var(--border); background: #fff; border-radius: 6px; padding: 6px 14px; cursor: pointer; font-size: 13px; }
.tab-btn.active { background: var(--brand); color: #fff; border-color: var(--brand); }
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
.panel h3 { margin: 0 0 10px; font-size: 15px; }
.panel h4 { margin: 12px 0 8px; font-size: 14px; }
.version-block, .group-block, .report-block { margin-bottom: 16px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.check-item { display: flex; align-items: center; gap: 6px; margin: 6px 0; font-size: 13px; }
.capacity-input { width: 72px; padding: 4px 6px; border: 1px solid var(--border); border-radius: 6px; }
.item-line { font-size: 12px; margin: 2px 0; }
.ok-text { color: #067647; font-weight: 600; }
.warn-text { color: #b42318; font-weight: 600; }
.lookup-box, .report-box { margin-top: 12px; border: 1px dashed var(--border); border-radius: 8px; padding: 10px 14px; background: #fafbfd; }
.check-list { margin: 6px 0; padding-left: 18px; font-size: 12px; }
.check-list li { margin: 2px 0; }
select, .modal-field input { padding: 7px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
</style>
