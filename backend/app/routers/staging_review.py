"""暂存分区复核接口：查询批次分区明细、查看分区版本、补录与重新确认。

这些接口是工作台“暂存落地”页新增入口的后端支撑：
- ``GET /api/staging_area/partitions?batch_code=`` 按批次查询分区明细；
- ``GET /api/staging_area/partitions/{id}`` 单分区明细（含全部版本）；
- ``GET /api/staging_area/partitions/{id}/versions/{version}`` 打开某个不可变版本；
- ``POST /api/staging_area/partitions/{id}/append`` 补录迟到行（可同时确认）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import service_for

router = APIRouter(prefix="/api/staging_area", tags=["暂存分区复核"])


class AppendPayload(BaseModel):
    count: int = Field(default=1, ge=1, le=1000)
    confirm: bool = False


class LandPayload(BaseModel):
    partition_no: int | None = Field(default=None, ge=1)
    record_count: int = Field(default=1, ge=1, le=100000)
    start_index: int = Field(default=0, ge=0)
    expected_count: int | None = Field(default=None, ge=0)
    confirm: bool = False


def _service():
    service = service_for("staging_area")
    if service is None:  # pragma: no cover - 注册表固定包含暂存模块
        raise HTTPException(status_code=404, detail="暂存落地服务未注册")
    return service


@router.post("/batches/{batch_code}/land", response_model=dict)
def land_partition(batch_code: str, payload: LandPayload) -> dict[str, Any]:
    """把一个批次的数据落成指定序号的暂存分区；这是查询分区明细前的落地入口。

    同一批次多次调用可落成 P1、P2... 多个分区，``start_index`` 决定本分区
    从批次产物的第几行开始，``expected_count`` 登记分区应到行数。
    """
    service = _service()
    result = service.land_batch(
        batch_code.strip(),
        payload.record_count,
        partition_no=payload.partition_no,
        start_index=payload.start_index,
        expected_count=payload.expected_count,
    )
    if payload.confirm:
        confirmed = service.confirm_land(int(result["staging_id"]))
        result["confirmation"] = confirmed
        detail = service.partition_detail(int(result["staging_id"]))
        result["data_version"] = detail.get("data_version") if detail else ""
    return result


@router.get("/partitions", response_model=dict)
def list_partitions(batch_code: str = Query(..., min_length=1)) -> dict[str, Any]:
    """按批次查询暂存分区明细，供操作员筛选复核对象。"""
    partitions = _service().list_batch_partitions(batch_code.strip())
    return {
        "batch_code": batch_code,
        "items": partitions,
        "total": len(partitions),
        "reviewable": sum(
            1 for p in partitions
            if p.get("latest_confirmed_version") or p.get("latest_version")),
    }


@router.get("/partitions/{staging_id}", response_model=dict)
def get_partition(staging_id: int) -> dict[str, Any]:
    """单分区明细：当前数据版本、确认版本与完整版本列表。"""
    detail = _service().partition_detail(staging_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"暂存分区 {staging_id} 不存在")
    return detail


@router.get("/partitions/{staging_id}/versions/{version}", response_model=dict)
def get_partition_version(staging_id: int, version: int) -> dict[str, Any]:
    """打开某个分区版本；返回版本元数据与该版本冻结的完整数据行。"""
    service = _service()
    entry = service.get_entry(staging_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"暂存分区 {staging_id} 不存在")
    payload = service.read_version(entry["暂存编号"], version)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail=f"暂存分区 {entry['暂存编号']} 版本 v{version} 不存在")
    return payload


@router.post("/partitions/{staging_id}/append", response_model=dict)
def append_partition_rows(staging_id: int, payload: AppendPayload) -> dict[str, Any]:
    """补录迟到行到分区；``confirm=true`` 时立即生成下一个确认版本。"""
    result = _service().append_rows(
        staging_id, count=payload.count, confirm=payload.confirm)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result
