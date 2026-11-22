"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import service_for

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/{module}/review", response_model=dict)
def get_review_list(
    module: str,
    entity_number: str | None = Query(default=None, alias="实体编号"),
    version: str | None = Query(default=None, alias="实体版本"),
    ingest_id: str | None = Query(default=None, alias="入库记录编号"),
) -> dict[str, Any]:
    """实体链接待复核清单 / 关系建模待审清单：都从共享实体证据派生。"""
    service = require_service(module)
    reviewer = getattr(service, "review_list", None)
    if reviewer is not None:
        return reviewer(entity_number=entity_number, version=version)
    pending = getattr(service, "pending_list", None)
    if pending is not None:
        return pending(ingest_id=ingest_id, version=version)
    raise HTTPException(status_code=404, detail=f"{module} 没有待审清单")


@router.get("/{module}/recover", response_model=dict)
def recover_candidates(module: str) -> dict[str, Any]:
    """重新打开链接工作台：恢复没有版本回指的旧链接候选。"""
    service = require_service(module)
    recover = getattr(service, "recover_candidates", None)
    if recover is None:
        raise HTTPException(status_code=404, detail=f"{module} 没有可恢复的旧候选")
    return recover()


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.get("/{module}/{entry_id}/evidence", response_model=dict)
def get_evidence_inputs(module: str, entry_id: int, ref: str | None = Query(default=None)) -> dict[str, Any]:
    """任一路都能查到实际用于生成它的实体证据输入。"""
    service = require_service(module)
    lookup = getattr(service, "evidence_inputs_for", None)
    if lookup is None:
        raise HTTPException(status_code=404, detail=f"{module} 不消费实体证据")
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    inputs = lookup(entry, ref)
    if inputs is None:
        raise HTTPException(status_code=404, detail="未找到该记录对应的实体证据")
    return inputs


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)
