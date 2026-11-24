/** 到达对账与暂存分区复核接口封装：查询分区、生成清单、执行对账与历史复算。 */
import { request } from './client'
import type {
  HistoryItem,
  PageResult,
  PartitionDetail,
  PartitionListResult,
  PartitionVersionRows,
  ReconcileResult,
  RecomputeResult,
  ReviewManifest,
} from '@/data/types'

async function jsonOrThrow<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) {
    let detail = fallback
    try {
      const payload = (await response.json()) as { detail?: string }
      if (payload?.detail) detail = payload.detail
    } catch {
      /* 响应体不是 JSON 时保留兜底文案 */
    }
    throw new Error(detail)
  }
  return (await response.json()) as T
}

/** 把一个批次落成指定序号的暂存分区（可同时确认）。 */
export async function landPartition(
  batchCode: string,
  input: {
    partition_no?: number
    record_count: number
    start_index?: number
    expected_count?: number
    confirm?: boolean
  },
): Promise<{ staging_id: number; staging_code: string; records_landed: number; data_version?: string }> {
  const response = await request(`/api/staging_area/batches/${batchCode}/land`, {
    method: 'POST',
    body: JSON.stringify(input),
  })
  return jsonOrThrow(response, '分区落地失败')
}

/** 查询批次对应的暂存分区明细（复核清单的筛选入口）。 */
export async function listBatchPartitions(batchCode: string): Promise<PartitionListResult> {
  const query = new URLSearchParams({ batch_code: batchCode }).toString()
  const response = await request(`/api/staging_area/partitions?${query}`)
  return jsonOrThrow<PartitionListResult>(response, '暂存分区明细读取失败')
}

export async function getPartitionDetail(stagingId: number | string): Promise<PartitionDetail> {
  const response = await request(`/api/staging_area/partitions/${stagingId}`)
  return jsonOrThrow<PartitionDetail>(response, '暂存分区明细读取失败')
}

/** 打开某个分区版本（不可变快照）及其完整数据行。 */
export async function getPartitionVersion(
  stagingId: number | string,
  version: number,
): Promise<PartitionVersionRows> {
  const response = await request(`/api/staging_area/partitions/${stagingId}/versions/${version}`)
  return jsonOrThrow<PartitionVersionRows>(response, '分区版本读取失败')
}

/** 向分区补录迟到行；confirm=true 时立即生成新的确认版本。 */
export async function appendPartitionRows(
  stagingId: number | string,
  count = 1,
  confirm = false,
): Promise<{ staging_code: string; record_count: number; data_version: string }> {
  const response = await request(`/api/staging_area/partitions/${stagingId}/append`, {
    method: 'POST',
    body: JSON.stringify({ count, confirm }),
  })
  return jsonOrThrow(response, '补录失败')
}

/** 生成可复核对账对象清单。 */
export async function buildReviewManifest(input: {
  batch_code: string
  staging_codes?: string[]
  include_unlanded?: boolean
  note?: string
}): Promise<ReviewManifest> {
  const response = await request('/api/arrival_reconcile/manifests', {
    method: 'POST',
    body: JSON.stringify(input),
  })
  return jsonOrThrow<ReviewManifest>(response, '可复核清单生成失败')
}

export async function listReviewManifests(batchCode?: string): Promise<ReviewManifest[]> {
  const query = batchCode ? `?${new URLSearchParams({ batch_code: batchCode }).toString()}` : ''
  const response = await request(`/api/arrival_reconcile/manifests${query}`)
  const payload = await jsonOrThrow<{ items: ReviewManifest[] }>(response, '可复核清单读取失败')
  return payload.items
}

/** 按可复核清单执行对账。 */
export async function executeManifest(manifestCode: string): Promise<ReconcileResult> {
  const response = await request('/api/arrival_reconcile/manifests/execute', {
    method: 'POST',
    body: JSON.stringify({ manifest_code: manifestCode }),
  })
  return jsonOrThrow<ReconcileResult>(response, '按清单执行对账失败')
}

export async function listReconcileHistory(batchCode?: string): Promise<HistoryItem[]> {
  const query = batchCode ? `?${new URLSearchParams({ batch_code: batchCode }).toString()}` : ''
  const response = await request(`/api/arrival_reconcile/history${query}`)
  const payload = await jsonOrThrow<{ items: HistoryItem[] }>(response, '对账历史读取失败')
  return payload.items
}

export async function getReconcileSnapshot(reconId: number | string): Promise<Record<string, unknown>> {
  const response = await request(`/api/arrival_reconcile/${reconId}/snapshot`)
  return jsonOrThrow<Record<string, unknown>>(response, '对账快照读取失败')
}

/** 按留存的分区版本逐行复算实到和缺失。 */
export async function recomputeReconciliation(reconId: number | string): Promise<RecomputeResult> {
  const response = await request(`/api/arrival_reconcile/${reconId}/recompute`, {
    method: 'POST',
  })
  return jsonOrThrow<RecomputeResult>(response, '对账复算失败')
}

export type { PageResult }
