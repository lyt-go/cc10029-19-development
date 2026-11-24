<template>
  <section class="review-panel" data-testid="staging-review-panel">
    <header class="review-head">
      <div>
        <h3>批次暂存分区明细</h3>
        <p class="review-desc">
          按批次查出全部暂存分区及其不可变版本；补录只新增数据版本，已确认版本保持原样。
        </p>
      </div>
      <form class="review-query" @submit.prevent="loadPartitions">
        <input
          v-model="batchCode"
          data-testid="staging-batch-input"
          placeholder="输入批次编号，如 TRAN-0001"
        />
        <button class="btn" data-testid="staging-land-open" type="button" @click="openLanding">
          落地分区
        </button>
        <button class="btn primary" data-testid="staging-query" type="submit">查询分区</button>
      </form>
    </header>

    <form v-if="showLanding" class="analysis-form" data-testid="landing-form" @submit.prevent="submitLanding">
    <label>
      分区序号
      <input v-model.number="landing.partition_no" type="number" min="1" data-testid="landing-partition-no" />
    </label>
    <label>
      本次落地行数
      <input v-model.number="landing.record_count" type="number" min="1" data-testid="landing-record-count" />
    </label>
    <label>
      起始行号（从 0 计）
      <input v-model.number="landing.start_index" type="number" min="0" data-testid="landing-start-index" />
    </label>
    <label>
      应到行数
      <input v-model.number="landing.expected_count" type="number" min="0" data-testid="landing-expected-count" />
    </label>
    <label class="inline-check">
      <input v-model="landing.confirm" type="checkbox" data-testid="landing-confirm" />
      落地后立即确认
    </label>
    <button class="btn primary" data-testid="landing-submit" type="submit">确认落地</button>
  </form>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <table v-if="partitions.length" class="data-table">
      <thead>
        <tr>
          <th>分区</th>
          <th>状态</th>
          <th>当前数据版本</th>
          <th>确认版本 / 记录数</th>
          <th>实际记录数</th>
          <th>待确认补录</th>
          <th>版本</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="part in partitions"
          :key="part.id"
          :data-testid="`partition-${part.staging_code}`"
        >
          <td>
            <strong>{{ part.staging_code }}</strong>
            <span class="muted"> / P{{ part.partition_no || '—' }}</span>
          </td>
          <td>{{ part.status }}</td>
          <td>{{ part.data_version || '—' }}</td>
          <td>{{ part.confirmed_version || '未确认' }} · {{ part.confirmed_count }} 行</td>
          <td>{{ part.record_count }} 行</td>
          <td>
            <span :class="part.pending_append ? 'tag warn' : 'tag'">
              {{ part.pending_append ? '有' : '无' }}
            </span>
          </td>
          <td class="version-list">
            <button
              v-for="version in part.versions"
              :key="version.version"
              class="link"
              type="button"
              :data-testid="`open-${part.staging_code}-v${version.version}`"
              @click="openVersion(part, version.version)"
            >
              v{{ version.version }}（{{ version.record_count }} 行{{ version.confirmed ? '·已确认' : '' }}）
            </button>
            <span v-if="!part.versions.length" class="muted">暂无版本</span>
          </td>
          <td class="row-actions">
            <button
              class="link"
              type="button"
              :data-testid="`append-${part.staging_code}`"
              @click="appendRow(part)"
            >
              补录 1 行
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`append-confirm-${part.staging_code}`"
              @click="appendRow(part, true)"
            >
              补录并确认
            </button>
          </td>
        </tr>
      </tbody>
    </table>
    <p v-else class="empty-state">输入批次编号后查询暂存分区，可看到每个分区的数据版本与确认结果。</p>

    <p v-if="lastMessage" class="review-message" data-testid="staging-message">{{ lastMessage }}</p>

    <div v-if="activeVersion" class="modal-backdrop" data-testid="version-modal">
      <div class="modal wide">
        <header class="modal-head">
          <h3>
            {{ activeVersion.staging_code }} · v{{ activeVersion.version }}
            <span class="muted">{{ activeVersion.confirmed ? '已确认' : '未确认' }}</span>
          </h3>
          <button class="btn ghost" type="button" @click="activeVersion = null">关闭</button>
        </header>
        <p class="review-desc">
          版本冻结时间：{{ formatTime(activeVersion.created_at) }} ·
          校验值 {{ activeVersion.checksum }} ·
          共 {{ activeVersion.record_count }} 行
        </p>
        <table class="data-table compact">
          <thead>
            <tr><th>#</th><th>记录主键</th><th>行号</th><th>字段摘要</th></tr>
          </thead>
          <tbody>
            <tr v-for="(row, index) in activeVersion.rows" :key="String(row.record_key ?? index)">
              <td>{{ index + 1 }}</td>
              <td>{{ row.record_key ?? '—' }}</td>
              <td>{{ row.line_no ?? '—' }}</td>
              <td class="muted">{{ summarize(row) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import {
  appendPartitionRows,
  getPartitionVersion,
  landPartition,
  listBatchPartitions,
} from '@/api/review-service'
import type { EntryRow, PartitionDetail, PartitionVersionRows } from '@/data/types'

const batchCode = ref('')
const partitions = ref<PartitionDetail[]>([])
const errorMessage = ref('')
const lastMessage = ref('')
const activeVersion = ref<PartitionVersionRows | null>(null)
const showLanding = ref(false)
const landing = ref({
  partition_no: 1,
  record_count: 4,
  start_index: 0,
  expected_count: 4,
  confirm: true,
})

function openLanding() {
  if (!batchCode.value.trim()) {
    errorMessage.value = '请先输入批次编号'
    return
  }
  errorMessage.value = ''
  showLanding.value = !showLanding.value
}

async function submitLanding() {
  errorMessage.value = ''
  try {
    const result = await landPartition(batchCode.value.trim(), {
      partition_no: landing.value.partition_no,
      record_count: landing.value.record_count,
      start_index: landing.value.start_index,
      expected_count: landing.value.expected_count,
      confirm: landing.value.confirm,
    })
    lastMessage.value =
      `${result.staging_code} 已落地 ${result.records_landed} 行` +
      (result.data_version ? `，当前数据版本 ${result.data_version}` : '')
    showLanding.value = false
    await loadPartitions()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分区落地失败'
  }
}

function summarize(row: EntryRow): string {
  const pairs = Object.entries(row)
    .filter(([key]) => !['id', 'status', 'pending', 'abnormal', 'record_key', 'line_no'].includes(key))
    .slice(0, 3)
    .map(([key, value]) => `${key}=${String(value)}`)
  return pairs.join('，') || '—'
}

function formatTime(value?: number): string {
  if (!value) return '—'
  return new Date(value * 1000).toLocaleString()
}

async function loadPartitions() {
  if (!batchCode.value.trim()) {
    errorMessage.value = '请先输入批次编号'
    return
  }
  errorMessage.value = ''
  lastMessage.value = ''
  try {
    const payload = await listBatchPartitions(batchCode.value.trim())
    partitions.value = payload.items
    if (!payload.items.length) {
      lastMessage.value = `批次 ${payload.batch_code} 下暂无暂存分区`
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分区查询失败'
  }
}

async function openVersion(part: PartitionDetail, version: number) {
  errorMessage.value = ''
  try {
    activeVersion.value = await getPartitionVersion(part.id, version)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版本读取失败'
  }
}

async function appendRow(part: PartitionDetail, confirm = false) {
  errorMessage.value = ''
  try {
    const result = await appendPartitionRows(part.id, 1, confirm)
    lastMessage.value = confirm
      ? `已向 ${result.staging_code} 补录 1 行并生成确认版本 ${result.data_version}`
      : `已向 ${result.staging_code} 补录 1 行，当前数据版本 ${result.data_version}（未确认）`
    await loadPartitions()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补录失败'
  }
}
</script>
