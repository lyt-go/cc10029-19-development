"""到达对账复核接口：生成可复核清单、按清单执行对账、历史复算与版本对读。

链路：查询批次分区 -> 生成清单（钉住每个分区的数据版本）-> 按清单建单执行
-> 历史列表复算 -> 分别打开两个确认版本。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import service_for

router = APIRouter(prefix="/api/arrival_reconcile", tags=["到达对账复核"])


class ManifestPayload(BaseModel):
    batch_code: str = Field(min_length=1)
    staging_codes: list[str] = Field(default_factory=list)
    include_unlanded: bool = False
    operator: str = "数据工程组"
    note: str = ""


class ExecuteManifestPayload(BaseModel):
    manifest_code: str = Field(min_length=1)
    operator: str = "数据工程组"


def _service():
    service = service_for("arrival_reconcile")
    if service is None:  # pragma: no cover - 注册表固定包含对账模块
        raise HTTPException(status_code=404, detail="到达对账服务未注册")
    return service


def _reconcile_or_404(recon_id: int):
    service = _service()
    entry = service.get_entry(recon_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"对账单 {recon_id} 不存在")
    return service, entry


@router.post("/manifests", response_model=dict)
def build_manifest(payload: ManifestPayload) -> dict[str, Any]:
    """查询批次对应的暂存分区明细并生成可复核对账对象清单。"""
    result = _service().build_review_manifest(
        payload.batch_code.strip(),
        staging_codes=payload.staging_codes or None,
        include_unlanded=payload.include_unlanded,
        operator=payload.operator,
        note=payload.note,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/manifests", response_model=dict)
def list_manifests(batch_code: str | None = Query(default=None)) -> dict[str, Any]:
    """按批次列出已生成的可复核清单。"""
    items = _service().list_manifests(batch_code.strip() if batch_code else None)
    return {"items": items, "total": len(items)}


@router.get("/manifests/{manifest_code}", response_model=dict)
def get_manifest(manifest_code: str) -> dict[str, Any]:
    payload = _service().get_manifest(manifest_code)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"可复核清单 {manifest_code} 不存在")
    return payload


@router.post("/manifests/execute", response_model=dict)
def execute_manifest(payload: ExecuteManifestPayload) -> dict[str, Any]:
    """按可复核清单建单并执行对账。"""
    result = _service().execute_from_manifest(
        payload.manifest_code.strip(), operator=payload.operator)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/history", response_model=dict)
def list_history(batch_code: str | None = Query(default=None)) -> dict[str, Any]:
    """对账历史列表：每份对账保留清单编号与逐行结果快照。"""
    service = _service()
    items, total = service.list_entries(filters=None, page=1, size=10_000)
    if batch_code:
        items = [i for i in items if str(i.get("批次编号", "")) == batch_code.strip()]
        total = len(items)
    history = []
    for item in items:
        snapshot = service.get_reconciliation_snapshot(int(item["id"]))
        history.append({
            **item,
            "manifest_code": snapshot.get("manifest_code") if snapshot else "",
            "used_alternate_version": bool(
                snapshot.get("used_alternate_version")) if snapshot else False,
            "partitions": [
                {
                    "staging_code": p.get("staging_code"),
                    "staging_id": p.get("staging_id"),
                    "partition_no": p.get("partition_no"),
                    "data_version": p.get("data_version"),
                    "resolved_version": p.get("resolved_version"),
                    "pinned_version": p.get("pinned_version"),
                    "actual_count": p.get("actual_count"),
                    "checksum": p.get("checksum"),
                    "used_alternate_version": p.get("used_alternate_version"),
                    "available_versions": p.get("available_versions", []),
                    "confirmations": (p.get("confirmation") or {}).get("confirmations", []),
                }
                for p in (snapshot.get("partitions") if snapshot else [])
            ],
            "has_snapshot": snapshot is not None,
            "reconciled_display": snapshot.get("reconciled_display") if snapshot else "",
        })
    history.sort(key=lambda h: int(h["id"]), reverse=True)
    return {"items": history, "total": len(history)}


@router.get("/{recon_id}/snapshot", response_model=dict)
def get_snapshot(recon_id: int) -> dict[str, Any]:
    """对账结果快照：留存的分区、数据版本和逐行实到/缺失明细。"""
    service, _ = _reconcile_or_404(recon_id)
    snapshot = service.get_reconciliation_snapshot(recon_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"对账单 {recon_id} 没有留存快照")
    return snapshot


@router.post("/{recon_id}/recompute", response_model=dict)
def recompute(recon_id: int) -> dict[str, Any]:
    """按留存的分区版本逐行复算实到与缺失，校验与历史结论一致。"""
    service, _ = _reconcile_or_404(recon_id)
    result = service.recompute_reconciliation(recon_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result
