"""流式质量接口：维护质量规则，覆盖提交校验、标记生效、停用规则等动作。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.stream_quality import StreamQualityService

router = APIRouter(prefix="/api/stream_quality", tags=["流式质量"])

service = StreamQualityService()

LIST_FIELDS = ["规则编号", "监控主题", "校验类型", "告警阈值", "采样比例", "检测窗口", "阻断动作", "规则版本"]
STATUSES = ["草稿", "待校验", "已生效", "已停用"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已生效、已停用"),
    sort: str | None = Query(default=None, description="排序字段"),
    order: str = Query(default="asc", description="asc 或 desc"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、规则编号关键字与状态过滤流式质量列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status,
        sort=sort, order=order, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="stream_quality.csv"'})


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条质量规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"质量规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条质量规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="质量规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条质量规则执行提交校验、标记生效、停用规则；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/recheck")
def recheck_entry(entry_id: int) -> dict:
    """重新检查：消费该规则主题下的水位纠正回执，重新检查受影响窗口。"""
    summary, message = service.recheck_entry(entry_id)
    if summary is None:
        return {"ok": False, "message": message, "summary": None}
    return {"ok": True, "message": message, "summary": summary}


@router.get("/{entry_id}/checks")
def list_checks(entry_id: int) -> dict:
    """读取该规则的检查记录（含回执重检的受影响窗口与检查版本）。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"质量规则 {entry_id} 不存在或已归档")
    items = service.get_quality_results(entry_id)
    return {"items": items, "total": len(items)}
