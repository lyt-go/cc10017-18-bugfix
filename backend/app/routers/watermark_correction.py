"""水位纠正回执接口：查询回执、恢复中断写入。

回执由水位追踪模块在检测到事件时间水位倒退时生成，这里提供
可按条件检索、排序和翻页的查询，以及写入中断后的恢复入口。
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import PageResult
from app.services.watermark_correction import LIST_FIELDS, STATUSES, correction_service

router = APIRouter(prefix="/api/watermark_correction", tags=["水位纠正"])

service = correction_service


def query_filters(request: Request) -> dict[str, str]:
    """只接受回执声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_receipts(
    request: Request,
    keyword: str | None = Query(default=None, description="按回执编号检索"),
    status: str | None = Query(default=None, description="写入中、已生成、已交付、已应用"),
    sort: str | None = Query(default=None, description="排序字段"),
    order: str = Query(default="asc", description="asc 或 desc"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、回执编号关键字与状态过滤水位纠正回执列表。"""
    items, total = service.list_receipts(
        filters=query_filters(request), keyword=keyword, status=status,
        sort=sort, order=order, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_receipts(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{receipt_id} 之前。"""
    items, _ = service.list_receipts(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "版本", "受影响窗口", "status"])
    for item in items:
        row = []
        values = [
            item.get("id"),
            *(item.get(field, "") for field in LIST_FIELDS),
            item.get("版本", ""),
            ";".join(item.get("受影响窗口") or []),
            item.get("status", ""),
        ]
        for value in values:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="watermark_correction.csv"'})


@router.post("/resume")
def resume_receipts() -> dict:
    """恢复写入中断的水位纠正回执：按最后成功位置续写，避免重复写入。"""
    resumed = service.resume()
    applied = sum(1 for receipt in resumed if receipt.get("status") == "已应用")
    return {
        "ok": True,
        "message": f"恢复 {len(resumed)} 份中断回执，其中 {applied} 份已应用",
        "items": resumed,
    }


@router.get("/{receipt_id}", response_model=dict)
def get_receipt(receipt_id: int) -> dict:
    """读取单条水位纠正回执明细；不存在时给出可读的错误说明。"""
    receipt = service.get_receipt(receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"水位纠正回执 {receipt_id} 不存在或已归档")
    return receipt
