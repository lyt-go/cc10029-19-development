<template>
  <section class="review-panel" data-testid="reconcile-review-panel">
    <header class="review-head">
      <div>
        <h3>可复核对账对象清单</h3>
        <p class="review-desc">
          筛选批次的分区明细后生成清单：清单钉住选中的批次、分区与数据版本，
          对账只读清单指向的版本，不会合读两个版本。
        </p>
      </div>
      <div class="review-query">
        <input
          v-model="batchCode"
          data-testid="manifest-batch-input"
          placeholder="输入批次编号"
        />
        <button class="btn" data-testid="manifest-build" type="button" @click="buildManifest">
          生成可复核清单
        </button>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text" data-testid="review-error">{{ errorMessage }}</p>

    <table v-if="manifest" class="data-table" data-testid="manifest-table">
      <thead>
        <tr>
          <th>分区</th>
          <th>钉住版本</th>
          <th>钉住行数</th>
          <th>是否确认</th>
          <th>版本校验值</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="part in manifest.partitions"
          :key="part.staging_code"
          :data-testid="`manifest-partition-${part.staging_code}`"
        >
          <td>{{ part.staging_code }} / P{{ part.partition_no || '—' }}</td>
          <td>{{ part.data_version }}（v{{ part.version }}）</td>
          <td>{{ part.record_count }} 行</td>
          <td>{{ part.confirmed ? '已确认' : '未确认快照' }}</td>
          <td class="mono">{{ part.checksum }}</td>
        </tr>
      </tbody>
    </table>
    <p v-if="manifest" class="review-message">
      清单 {{ manifest.manifest_code }} · 应到合计 {{ manifest.expected_total }} 行 ·
      指纹 {{ manifest.manifest_checksum.slice(0, 12) }}
    </p>

    <div v-if="manifest" class="review-actions">
      <button class="btn primary" data-testid="manifest-execute" type="button" @click="execute">
        执行到达对账
      </button>
      <button class="btn" type="button" @click="clearManifest">重新筛选</button>
    </div>

    <template v-if="result">
      <div class="analysis-summary" data-testid="reconcile-summary">
        <article class="analysis-card">
          <span class="stat-label">应到</span>
          <strong class="stat-value">{{ result.expected }}</strong>
        </article>
        <article class="analysis-card">
          <span class="stat-label">实到</span>
          <strong class="stat-value">{{ result.actual }}</strong>
        </article>
        <article class="analysis-card">
          <span class="stat-label">缺失</span>
          <strong class="stat-value">{{ result.missing }}</strong>
        </article>
        <article class="analysis-card">
          <span class="stat-label">结论</span>
          <strong class="stat-value">{{ result.balanced ? '已平账' : '存在差异' }}</strong>
        </article>
      </div>
      <p class="review-message" data-testid="alternate-flag">
        本次{{ result.used_alternate_version ? '已改用另一个确认版本' : '未改用其他版本，全部按清单钉住版本读取' }}。
        <span v-for="item in result.alternate_details" :key="item.staging_code" class="muted">
          {{ item.staging_code }}：v{{ item.pinned_version }} → v{{ item.used_version }}（{{ item.reason }}）
        </span>
      </p>

      <table class="data-table" data-testid="reconcile-partitions">
        <thead>
          <tr>
            <th>分区</th>
            <th>钉住版本</th>
            <th>实际读取版本</th>
            <th>实到行数</th>
            <th>确认结果</th>
            <th>改用版本</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="part in result.partitions" :key="part.staging_code">
            <td>{{ part.staging_code }} / P{{ part.partition_no || '—' }}</td>
            <td>{{ part.pinned_data_version || `v${part.pinned_version}` }}</td>
            <td>{{ part.data_version }}</td>
            <td>{{ part.actual_count }} 行</td>
            <td>
              <span v-if="part.confirmation">
                {{ part.confirmation.confirmations.map((c) => `v${c.confirmed_version}·${c.record_count}行`).join('，') }}
              </span>
              <span v-else class="muted">无确认结果</span>
            </td>
            <td>
              <span :class="part.used_alternate_version ? 'tag warn' : 'tag'">
                {{ part.used_alternate_version ? '是' : '否' }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>

      <p v-if="result.missing_row_keys.length" class="error-text">
        缺失记录：{{ result.missing_row_keys.join('，') }}
      </p>
    </template>

    <section class="review-sub">
      <header class="review-head">
        <div>
          <h3>对账历史列表</h3>
          <p class="review-desc">每份对账留存清单与逐行快照，可按留存分区原样复算。</p>
        </div>
        <button class="btn ghost" type="button" data-testid="history-refresh" @click="loadHistory">
          刷新历史
        </button>
      </header>
      <table class="data-table" data-testid="history-table">
        <thead>
          <tr>
            <th>对账编号</th>
            <th>批次</th>
            <th>清单</th>
            <th>应到 / 实到</th>
            <th>缺失</th>
            <th>结论</th>
            <th>分区版本</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in history" :key="String(item.id)" :data-testid="`history-${item.recon_code ?? item.id}`">
            <td>{{ item['对账编号'] ?? item.id }}</td>
            <td>{{ item['批次编号'] }}</td>
            <td class="mono">{{ item.manifest_code || '—' }}</td>
            <td>{{ item['应到条数'] }} / {{ item['实到条数'] }}</td>
            <td>{{ item['缺失条数'] }}</td>
            <td>{{ item['对账结论'] }}</td>
            <td class="version-list">
              <span v-for="part in item.partitions" :key="part.staging_code" class="muted">
                {{ part.partition_no || part.staging_code }}:{{ part.data_version }}({{ part.actual_count }}行)
              </span>
            </td>
            <td class="row-actions">
              <button
                class="link"
                type="button"
                :data-testid="`recompute-${item.id}`"
                @click="recompute(item)"
              >
                逐行复算
              </button>
              <button
                class="link"
                type="button"
                :data-testid="`open-history-${item.id}`"
                @click="openHistory(item)"
              >
                打开版本
              </button>
            </td>
          </tr>
          <tr v-if="!history.length">
            <td colspan="8" class="empty-state">暂无对账历史</td>
          </tr>
        </tbody>
      </table>
      <p v-if="recomputeMessage" class="review-message" data-testid="recompute-message">
        {{ recomputeMessage }}
      </p>
    </section>

    <div v-if="historyDetail" class="modal-backdrop" data-testid="history-modal">
      <div class="modal wide">
        <header class="modal-head">
          <h3>{{ historyDetail.recon_code }} · 留存分区与版本</h3>
          <button class="btn ghost" type="button" @click="closeHistory">关闭</button>
        </header>
        <p class="review-desc">
          以下版本来自对账时刻的留存快照，逐行复算与打开都只读这些版本，不受当前产物影响。
        </p>
        <table
          v-for="part in historyDetail.partitions"
          :key="part.staging_code"
          class="data-table compact"
        >
          <thead>
            <tr>
              <th colspan="5">
                {{ part.staging_code }} / P{{ part.partition_no || '—' }}
                <span class="muted">
                  本次读取 {{ part.data_version }}（{{ part.actual_count }} 行）
                  <template v-if="part.used_alternate_version">· 已改用另一个确认版本</template>
                </span>
              </th>
            </tr>
            <tr>
              <th>版本</th>
              <th>类型</th>
              <th>行数</th>
              <th>校验值</th>
              <th>打开</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="version in part.available_versions"
              :key="`${part.staging_code}-${version.version}`"
              :data-testid="`history-version-${part.staging_code}-v${version.version}`"
            >
              <td>
                {{ version.data_version }}
                <span v-if="version.data_version === part.data_version" class="tag">本次读取</span>
              </td>
              <td>{{ version.confirmed ? '确认结果' : version.kind }}</td>
              <td>{{ version.record_count }} 行</td>
              <td class="mono">{{ version.checksum }}</td>
              <td>
                <button
                  class="link"
                  type="button"
                  :data-testid="`view-${part.staging_code}-v${version.version}`"
                  @click="viewVersion(part, version.version)"
                >
                  查看行明细
                </button>
              </td>
            </tr>
            <tr v-if="!part.available_versions.length">
              <td colspan="5" class="empty-state">该分区在留存快照中没有可打开的版本</td>
            </tr>
          </tbody>
        </table>
        <div v-if="versionRows" class="version-rows" data-testid="version-rows">
          <h4>
            {{ versionRows.staging_code }} · {{ versionRows.data_version }}
            （{{ versionRows.rows.length }} 行{{ versionRows.confirmed ? '·已确认' : '' }}）
          </h4>
          <table class="data-table compact">
            <thead>
              <tr><th>#</th><th>记录主键</th><th>行号</th></tr>
            </thead>
            <tbody>
              <tr v-for="(row, index) in versionRows.rows" :key="String(row.record_key ?? index)">
                <td>{{ index + 1 }}</td>
                <td>{{ row.record_key ?? '—' }}</td>
                <td>{{ row.line_no ?? '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  buildReviewManifest,
  executeManifest,
  getPartitionVersion,
  listReconcileHistory,
  recomputeReconciliation,
} from '@/api/review-service'
import type {
  HistoryItem,
  HistoryPartition,
  PartitionVersionRows,
  ReconcileResult,
  ReviewManifest,
} from '@/data/types'

const batchCode = ref('')
const manifest = ref<ReviewManifest | null>(null)
const result = ref<ReconcileResult | null>(null)
const history = ref<HistoryItem[]>([])
const errorMessage = ref('')
const recomputeMessage = ref('')
const historyDetail = ref<{
  recon_code: string
  partitions: HistoryPartition[]
} | null>(null)
const versionRows = ref<PartitionVersionRows | null>(null)

function clearManifest() {
  manifest.value = null
  result.value = null
  errorMessage.value = ''
}

async function buildManifest() {
  if (!batchCode.value.trim()) {
    errorMessage.value = '请先输入批次编号'
    return
  }
  errorMessage.value = ''
  result.value = null
  try {
    manifest.value = await buildReviewManifest({ batch_code: batchCode.value.trim() })
  } catch (error) {
    manifest.value = null
    errorMessage.value = error instanceof Error ? error.message : '清单生成失败'
  }
}

async function execute() {
  if (!manifest.value) return
  errorMessage.value = ''
  try {
    result.value = await executeManifest(manifest.value.manifest_code)
    await loadHistory()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '对账执行失败'
  }
}

async function loadHistory() {
  try {
    history.value = await listReconcileHistory(
      batchCode.value.trim() ? batchCode.value.trim() : undefined,
    )
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '对账历史读取失败'
  }
}

async function recompute(item: HistoryItem) {
  recomputeMessage.value = ''
  try {
    const outcome = await recomputeReconciliation(String(item.id))
    recomputeMessage.value =
      `${outcome.recon_code} 复算：应到 ${outcome.expected}，实到 ${outcome.actual}，` +
      `缺失 ${outcome.missing}，与留存快照${outcome.matches_snapshot ? '一致' : '不一致'}，` +
      `${outcome.used_alternate_version ? '当日曾改用另一个确认版本' : '当日未改用其他版本'}`
  } catch (error) {
    recomputeMessage.value = error instanceof Error ? error.message : '复算失败'
  }
}

async function openHistory(item: HistoryItem) {
  versionRows.value = null
  errorMessage.value = ''
  try {
    // 历史快照已留存每个分区的全部可打开版本（含 4 行版与 5 行版），直接使用
    historyDetail.value = {
      recon_code: String(item['对账编号'] ?? item.id),
      partitions: item.partitions || [],
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '历史版本读取失败'
  }
}

function closeHistory() {
  historyDetail.value = null
  versionRows.value = null
}

async function viewVersion(part: HistoryPartition, version: number) {
  errorMessage.value = ''
  try {
    if (part.staging_id === undefined) {
      errorMessage.value = `分区 ${part.staging_code} 缺少分区 ID，无法打开版本`
      return
    }
    versionRows.value = await getPartitionVersion(part.staging_id, version)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版本读取失败'
  }
}

onMounted(loadHistory)
</script>
