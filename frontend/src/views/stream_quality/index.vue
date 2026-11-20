<template>
  <section class="page" data-module="stream_quality">
    <header class="page-head">
      <div>
        <h2>流式质量管理</h2>
        <p class="page-desc">维护流式数据实时完整性、延迟与分布的质量监控规则，接入水位纠正回执并重新检查受影响窗口。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记质量规则</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出流式质量清单</button>
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
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
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
            <button class="link" type="button" :data-testid="`recheck-${row.id}`" @click="recheckRow(row)">重新检查</button>
            <button class="link" type="button" :data-testid="`checks-${row.id}`" @click="openChecks(row)">检查记录</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无流式质量数据，可先登记质量规则</td>
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

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记质量规则</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="showChecks" class="modal-backdrop" data-testid="checks-modal">
      <div class="modal">
        <h3>检查记录 · {{ checksTarget?.['规则编号'] }}</h3>
        <form class="filter-bar" @submit.prevent>
          <label class="filter-item">
            <span>受影响窗口</span>
            <input v-model="windowFilter" data-testid="checks-window-filter" placeholder="按窗口筛选，如 2026-11-16T10:05" />
          </label>
        </form>
        <table class="data-table">
          <thead>
            <tr>
              <th>检查时间</th>
              <th>检查版本</th>
              <th>类型</th>
              <th>延迟记录数</th>
              <th>告警阈值</th>
              <th>结果</th>
              <th>违规</th>
              <th>受影响窗口</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(check, index) in filteredChecks" :key="index" :data-testid="`check-row-${index}`">
              <td>{{ check.checked_at }}</td>
              <td>{{ check['检查版本'] ?? '—' }}</td>
              <td>
                <span v-if="check.recheck" class="badge badge-warn">回执重检</span>
                <span v-else>常规检查</span>
              </td>
              <td>{{ check.late_count }}</td>
              <td>{{ check.threshold }}</td>
              <td>
                <span v-if="check.passed" class="badge badge-ok">通过</span>
                <span v-else class="badge badge-warn">告警</span>
              </td>
              <td>{{ (check.violations || []).length }}</td>
              <td>
                <div class="window-chips">
                  <span v-for="window in check['受影响窗口'] || []" :key="window" class="window-chip">{{ window }}</span>
                  <span v-if="!(check['受影响窗口'] || []).length">—</span>
                </div>
              </td>
            </tr>
            <tr v-if="!filteredChecks.length">
              <td colspan="8" class="empty-state">暂无检查记录，执行「重新检查」或「标记生效」后在此查看</td>
            </tr>
          </tbody>
        </table>
        <div class="page-actions">
          <button class="btn" data-testid="close-checks" type="button" @click="showChecks = false">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条流式质量记录</span>
      <span v-if="infoMessage" class="info-text" data-testid="info-message">{{ infoMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/stream_quality'
const columns = ["规则编号", "监控主题", "校验类型", "告警阈值", "采样比例", "检测窗口", "阻断动作", "规则版本"]
const actions = ["提交校验", "标记生效", "停用规则"]
const statuses = ["草稿", "待校验", "已生效", "已停用"]
const metricNames = ["质量规则总数", "待校验规则", "已生效规则"]

const rows = ref<Row[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(20)
const sortField = ref('')
const sortOrder = ref<'asc' | 'desc'>('asc')
const errorMessage = ref('')
const infoMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const showChecks = ref(false)
const checksTarget = ref<Row | null>(null)
const checks = ref<Row[]>([])
const windowFilter = ref('')

const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[2]).length },
])
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / size.value)))
const pageNumbers = computed(() => {
  const start = Math.max(1, Math.min(page.value - 3, pageCount.value - 6))
  const end = Math.min(pageCount.value, start + 6)
  const list: number[] = []
  for (let p = start; p <= end; p += 1) list.push(p)
  return list
})
const filteredChecks = computed(() => {
  const keyword = windowFilter.value.trim()
  if (!keyword) return checks.value
  return checks.value.filter((check) =>
    (check['受影响窗口'] || []).some((window: string) => window.includes(keyword)))
})

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
  page.value = 1
  void reload()
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters.value)) {
    if (value) params.set(key, value)
  }
  params.set('page', String(page.value))
  params.set('size', String(size.value))
  if (sortField.value) {
    params.set('sort', sortField.value)
    params.set('order', sortOrder.value)
  }
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    if (!response.ok) {
      throw new Error('质量规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '流式质量列表读取失败'
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
    link.download = 'stream_quality.csv'
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
      throw new Error('流式质量动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '流式质量操作失败'
  }
}

async function recheckRow(row: Row) {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/recheck`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '重新检查失败')
    infoMessage.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重新检查失败'
  }
}

async function openChecks(row: Row) {
  errorMessage.value = ''
  windowFilter.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/checks`)
    if (!response.ok) throw new Error('检查记录读取失败')
    const payload = await response.json()
    checks.value = payload.items ?? []
    checksTarget.value = row
    showChecks.value = true
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '检查记录读取失败'
  }
}

onMounted(reload)
</script>
