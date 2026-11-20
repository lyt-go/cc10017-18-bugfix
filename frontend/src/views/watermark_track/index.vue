<template>
  <section class="page" data-module="watermark_track">
    <header class="page-head">
      <div>
        <h2>水位追踪管理</h2>
        <p class="page-desc">维护流处理事件时间与处理时间的双水位追踪，定位消费偏移前进但事件时间水位倒退的颠倒记录，并生成水位纠正回执。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记水位标记</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出水位追踪清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="search">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item checkbox">
        <input v-model="onlyInverted" data-testid="filter-inverted" type="checkbox" />
        仅看颠倒记录
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column" class="sortable" @click="toggleSort(column)">
            {{ column }}<span v-if="sortField === column" class="sort-mark">{{ sortOrder === 'asc' ? '▲' : '▼' }}</span>
          </th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">
            <template v-if="column === '标记编号'">
              {{ row[column] ?? '—' }}
              <span v-if="row.inverted" class="badge badge-warn" data-testid="inverted-badge">颠倒</span>
            </template>
            <template v-else>{{ row[column] ?? '—' }}</template>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" :data-testid="`correct-${row.id}`" @click="correctRow(row)">水位纠正</button>
            <button class="link" type="button" :data-testid="`report-${row.id}`" @click="openReport(row)">上报读数</button>
            <button class="link" type="button" :data-testid="`detail-${row.id}`" @click="openDetail(row)">详情</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无水位追踪数据，可先登记水位标记</td>
        </tr>
      </tbody>
    </table>

    <div class="pagination" data-testid="pagination">
      <button class="btn ghost" type="button" :disabled="page <= 1" @click="gotoPage(page - 1)">上一页</button>
      <button
        v-for="p in pageNumbers"
        :key="p"
        class="btn ghost"
        :class="{ active: p === page }"
        type="button"
        @click="gotoPage(p)"
      >
        {{ p }}
      </button>
      <button class="btn ghost" type="button" :disabled="page >= pageCount" @click="gotoPage(page + 1)">下一页</button>
      <select v-model.number="size" data-testid="page-size" @change="search">
        <option :value="10">10 条/页</option>
        <option :value="20">20 条/页</option>
        <option :value="50">50 条/页</option>
      </select>
    </div>

    <section class="panel-section" data-testid="receipt-section">
      <h3>水位纠正回执</h3>
      <form class="filter-bar" @submit.prevent="searchReceipts">
        <label class="filter-item">
          <span>回执编号</span>
          <input v-model="receiptKeyword" data-testid="receipt-keyword" placeholder="按回执编号检索" />
        </label>
        <label class="filter-item">
          <span>数据主题</span>
          <input v-model="receiptFilters['数据主题']" data-testid="receipt-filter-数据主题" placeholder="按数据主题检索" />
        </label>
        <label class="filter-item">
          <span>分区编号</span>
          <input v-model="receiptFilters['分区编号']" data-testid="receipt-filter-分区编号" placeholder="按分区编号检索" />
        </label>
        <label class="filter-item">
          <span>状态</span>
          <select v-model="receiptStatus" data-testid="receipt-filter-status">
            <option value="">全部</option>
            <option v-for="s in receiptStatuses" :key="s" :value="s">{{ s }}</option>
          </select>
        </label>
        <button class="btn" data-testid="receipt-query" type="submit">查询</button>
        <button class="btn ghost" data-testid="receipt-reset" type="button" @click="resetReceiptFilters">重置条件</button>
        <button class="btn" data-testid="resume-receipts" type="button" @click="resumeReceipts">恢复中断回执</button>
      </form>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in receiptColumns" :key="column" class="sortable" @click="toggleReceiptSort(column)">
              {{ column }}<span v-if="receiptSortField === column" class="sort-mark">{{ receiptSortOrder === 'asc' ? '▲' : '▼' }}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="receipt in receipts" :key="String(receipt.id)" :data-testid="`receipt-row-${receipt.id}`">
            <td>{{ receipt['回执编号'] }}</td>
            <td>{{ receipt['数据主题'] }}</td>
            <td>{{ receipt['分区编号'] }}</td>
            <td>{{ receipt['旧检查版本'] }}</td>
            <td>{{ receipt['检查版本'] }}</td>
            <td>v{{ receipt['版本'] }}</td>
            <td>{{ receipt['纠正前事件时间水位'] }} → {{ receipt['纠正后事件时间水位'] }}</td>
            <td>
              <span class="window-chips" :title="(receipt['受影响窗口'] || []).join('\n')">
                {{ (receipt['受影响窗口'] || []).length }} 个窗口
              </span>
            </td>
            <td>{{ receipt['冲突判定'] }}</td>
            <td>{{ receipt['最后成功位置'] }}</td>
            <td>{{ receipt['status'] }}</td>
          </tr>
          <tr v-if="!receipts.length">
            <td :colspan="receiptColumns.length" class="empty-state">暂无水位纠正回执，对颠倒记录执行「水位纠正」后在此查看</td>
          </tr>
        </tbody>
      </table>
      <div class="pagination" data-testid="receipt-pagination">
        <button class="btn ghost" type="button" :disabled="receiptPage <= 1" @click="gotoReceiptPage(receiptPage - 1)">上一页</button>
        <button
          v-for="p in receiptPageNumbers"
          :key="p"
          class="btn ghost"
          :class="{ active: p === receiptPage }"
          type="button"
          @click="gotoReceiptPage(p)"
        >
          {{ p }}
        </button>
        <button class="btn ghost" type="button" :disabled="receiptPage >= receiptPageCount" @click="gotoReceiptPage(receiptPage + 1)">下一页</button>
        <span>共 {{ receiptTotal }} 份回执</span>
      </div>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记水位标记</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input
            v-model="createValues[field]"
            :data-testid="`create-${field}`"
            :required="field !== '检查版本'"
            :placeholder="field === '检查版本' ? '留空默认 v1' : ''"
          />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="showReport" class="modal-backdrop" data-testid="report-modal">
      <form class="modal" @submit.prevent="submitReport">
        <h3>上报水位读数 · {{ reportTarget?.['标记编号'] }}</h3>
        <label v-for="field in reportFields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="reportValues[field]" :data-testid="`report-${field}`" />
        </label>
        <p class="page-desc">事件时间水位只能按检查版本向前修正；携带旧基准版本的读数会触发冲突判定并生成回执新版本。</p>
        <div class="page-actions">
          <button class="btn" type="button" @click="showReport = false">取消</button>
          <button class="btn primary" data-testid="submit-report" type="submit" :disabled="reporting">
            {{ reporting ? '提交中…' : '确认上报' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="showDetail" class="modal-backdrop" data-testid="detail-modal">
      <div class="modal">
        <h3>水位标记详情 · {{ detailEntry?.['标记编号'] }}</h3>
        <dl v-if="detailEntry" class="detail-grid">
          <template v-for="field in columns" :key="field">
            <dt>{{ field }}</dt>
            <dd>{{ detailEntry[field] ?? '—' }}</dd>
          </template>
          <dt>颠倒记录</dt>
          <dd>
            <span v-if="detailEntry.inverted" class="badge badge-warn">是</span>
            <span v-else class="badge badge-ok">否</span>
          </dd>
          <dt>上一检查版本</dt>
          <dd>{{ prevCheckText }}</dd>
        </dl>
        <h4>水位纠正回执</h4>
        <table class="data-table">
          <thead>
            <tr>
              <th>回执编号</th>
              <th>旧检查版本</th>
              <th>检查版本</th>
              <th>事件时间水位纠正</th>
              <th>受影响窗口</th>
              <th>冲突判定</th>
              <th>最后成功位置</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="receipt in detailReceipts" :key="String(receipt.id)" :data-testid="`detail-receipt-${receipt.id}`">
              <td>{{ receipt['回执编号'] }}（v{{ receipt['版本'] }}）</td>
              <td>{{ receipt['旧检查版本'] }}</td>
              <td>{{ receipt['检查版本'] }}</td>
              <td>{{ receipt['纠正前事件时间水位'] }} → {{ receipt['纠正后事件时间水位'] }}</td>
              <td>
                <div class="window-chips">
                  <span v-for="window in receipt['受影响窗口'] || []" :key="window" class="window-chip">{{ window }}</span>
                </div>
              </td>
              <td>{{ receipt['冲突判定'] }}</td>
              <td>{{ receipt['最后成功位置'] }}</td>
              <td>{{ receipt['status'] }}</td>
            </tr>
            <tr v-if="!detailReceipts.length">
              <td colspan="8" class="empty-state">该标记暂无水位纠正回执</td>
            </tr>
          </tbody>
        </table>
        <div class="page-actions">
          <button class="btn" data-testid="close-detail" type="button" @click="showDetail = false">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条水位追踪记录</span>
      <span v-if="infoMessage" class="info-text" data-testid="info-message">{{ infoMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/watermark_track'
const RECEIPT_ENDPOINT = '/api/watermark_correction'
const columns = ["标记编号", "数据主题", "分区编号", "事件时间水位", "处理时间水位", "延迟记录数", "消费偏移", "检查时间", "检查版本"]
const actions = ["标记正常", "标记滞后", "停用追踪"]
const statuses = ["草稿", "正常", "滞后", "已停用"]
const metricNames = ["水位标记总数", "正常水位", "滞后水位"]
const receiptColumns = ["回执编号", "数据主题", "分区编号", "旧检查版本", "检查版本", "版本", "事件时间水位纠正", "受影响窗口", "冲突判定", "最后成功位置", "状态"]
const receiptStatuses = ["写入中", "已生成", "已交付", "已应用"]
const reportFields = ["消费偏移", "事件时间水位", "处理时间水位", "延迟记录数", "检查版本", "基准版本"]

const rows = ref<Row[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(20)
const sortField = ref('')
const sortOrder = ref<'asc' | 'desc'>('asc')
const onlyInverted = ref(false)
const errorMessage = ref('')
const infoMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})

const receipts = ref<Row[]>([])
const receiptTotal = ref(0)
const receiptPage = ref(1)
const receiptSize = ref(10)
const receiptKeyword = ref('')
const receiptStatus = ref('')
const receiptFilters = ref<Record<string, string>>({})
const receiptSortField = ref('')
const receiptSortOrder = ref<'asc' | 'desc'>('asc')

const showReport = ref(false)
const reporting = ref(false)
const reportTarget = ref<Row | null>(null)
const reportValues = ref<Record<string, string>>({})

const showDetail = ref(false)
const detailEntry = ref<Row | null>(null)
const detailReceipts = ref<Row[]>([])

const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[2]).length },
])
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / size.value)))
const pageNumbers = computed(() => pageList(page.value, pageCount.value))
const receiptPageCount = computed(() => Math.max(1, Math.ceil(receiptTotal.value / receiptSize.value)))
const receiptPageNumbers = computed(() => pageList(receiptPage.value, receiptPageCount.value))
const prevCheckText = computed(() => {
  const prev = detailEntry.value?._prev_check
  if (!prev) return '—'
  return `${prev['检查版本']} · 事件时间水位 ${prev['事件时间水位']} · 消费偏移 ${prev['消费偏移']}`
})

function pageList(current: number, count: number): number[] {
  const start = Math.max(1, Math.min(current - 3, count - 6))
  const end = Math.min(count, start + 6)
  const list: number[] = []
  for (let p = start; p <= end; p += 1) list.push(p)
  return list
}

function buildQuery(extra: Record<string, string> = {}): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters.value)) {
    if (value) params.set(key, value)
  }
  for (const [key, value] of Object.entries(extra)) {
    if (value) params.set(key, value)
  }
  return params.toString()
}

function toggleSort(column: string) {
  if (sortField.value === column) {
    sortOrder.value = sortOrder.value === 'asc' ? 'desc' : 'asc'
  } else {
    sortField.value = column
    sortOrder.value = 'asc'
  }
  void reload()
}

function gotoPage(p: number) {
  page.value = Math.min(Math.max(1, p), pageCount.value)
  void reload()
}

function search() {
  page.value = 1
  void reload()
}

function resetFilters() {
  filters.value = {}
  onlyInverted.value = false
  page.value = 1
  void reload()
}

async function reload() {
  errorMessage.value = ''
  const query = buildQuery({
    page: String(page.value),
    size: String(size.value),
    ...(sortField.value ? { sort: sortField.value, order: sortOrder.value } : {}),
    ...(onlyInverted.value ? { inverted: 'true' } : {}),
  })
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('水位标记列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '水位追踪列表读取失败'
  }
}

async function reloadReceipts() {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(receiptFilters.value)) {
    if (value) params.set(key, value)
  }
  if (receiptKeyword.value) params.set('keyword', receiptKeyword.value)
  if (receiptStatus.value) params.set('status', receiptStatus.value)
  if (receiptSortField.value) {
    params.set('sort', receiptSortField.value)
    params.set('order', receiptSortOrder.value)
  }
  params.set('page', String(receiptPage.value))
  params.set('size', String(receiptSize.value))
  try {
    const response = await request(`${RECEIPT_ENDPOINT}?${params.toString()}`)
    if (!response.ok) throw new Error('水位纠正回执读取失败')
    const payload = await response.json()
    receipts.value = payload.items ?? []
    receiptTotal.value = payload.total ?? receipts.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '水位纠正回执读取失败'
  }
}

function toggleReceiptSort(column: string) {
  const sortable: Record<string, string> = {
    事件时间水位纠正: '纠正后事件时间水位',
    状态: 'status',
  }
  const field = sortable[column] ?? column
  if (receiptSortField.value === field) {
    receiptSortOrder.value = receiptSortOrder.value === 'asc' ? 'desc' : 'asc'
  } else {
    receiptSortField.value = field
    receiptSortOrder.value = 'asc'
  }
  void reloadReceipts()
}

function gotoReceiptPage(p: number) {
  receiptPage.value = Math.min(Math.max(1, p), receiptPageCount.value)
  void reloadReceipts()
}

function searchReceipts() {
  receiptPage.value = 1
  void reloadReceipts()
}

function resetReceiptFilters() {
  receiptFilters.value = {}
  receiptKeyword.value = ''
  receiptStatus.value = ''
  receiptPage.value = 1
  void reloadReceipts()
}

async function resumeReceipts() {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const response = await request(`${RECEIPT_ENDPOINT}/resume`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '恢复失败')
    infoMessage.value = payload.message
    await Promise.all([reload(), reloadReceipts()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '恢复中断回执失败'
  }
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'watermark_track.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('水位追踪动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '水位追踪操作失败'
  }
}

async function correctRow(row: Row) {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/correct`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.message || '水位纠正失败')
    infoMessage.value = payload.message
    await Promise.all([reload(), reloadReceipts()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '水位纠正失败'
  }
}

function openReport(row: Row) {
  reportTarget.value = row
  reportValues.value = Object.fromEntries(reportFields.map((field) => [field, '']))
  errorMessage.value = ''
  showReport.value = true
}

async function submitReport() {
  if (!reportTarget.value) return
  reporting.value = true
  errorMessage.value = ''
  try {
    const body: Record<string, string> = {}
    for (const [key, value] of Object.entries(reportValues.value)) {
      if (value) body[key] = value
    }
    const response = await request(`${ENDPOINT}/${reportTarget.value.id}/report`, {
      method: 'POST', body: JSON.stringify(body),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '上报失败')
    infoMessage.value = payload.message
    showReport.value = false
    await Promise.all([reload(), reloadReceipts()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '上报读数失败'
  } finally {
    reporting.value = false
  }
}

async function openDetail(row: Row) {
  errorMessage.value = ''
  try {
    const [entryResp, receiptsResp] = await Promise.all([
      request(`${ENDPOINT}/${row.id}`),
      request(`${ENDPOINT}/${row.id}/corrections`),
    ])
    if (!entryResp.ok) throw new Error('水位标记详情读取失败')
    detailEntry.value = await entryResp.json()
    detailReceipts.value = receiptsResp.ok ? ((await receiptsResp.json()).items ?? []) : []
    showDetail.value = true
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '水位标记详情读取失败'
  }
}

onMounted(() => {
  void reload()
  void reloadReceipts()
})
</script>
