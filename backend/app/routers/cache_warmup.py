"""缓存重建批次包接口。

围绕「一次为多个目标生成缓存重建批次包」提供：
- 缓存目标/分组登记与容量调整；
- 多目标批次预热（部分失败逐项可查）；
- 同组多选、一次提交、逐条核对已生成版本与暂未纳入目标；
- 缓存条目查询与按参数/版本/有效期的命中；
- 总计+分组联合查询对账（同版本对回，拦截跨版本混算）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services.cache_warmup import (
    POLICY_LABELS,
    Q_GROUP,
    Q_TOTAL,
    SHARED_DATASET,
    VERSION_V1,
    VERSION_V2,
    WarmupService,
    warmup_service,
)

router = APIRouter(prefix="/api/cache-warmup", tags=["缓存重建批次包"])


class TargetCreate(BaseModel):
    name: str
    query_kind: str = Field(description="total=总计查询，group=分组查询")
    fixed_params: list[dict[str, Any]] = Field(default_factory=list)
    ttl_seconds: int = 3600
    max_capacity: int
    group_name: str | None = None


class CapacityUpdate(BaseModel):
    max_capacity: int


class BatchCreate(BaseModel):
    target_ids: list[str]
    data_version: str
    remark: str | None = None
    ttl_seconds: int | None = None


class GroupSubmit(BaseModel):
    target_ids: list[str] = Field(description="缓存入口多选后要纳入本次批次的目标")
    data_version: str
    ttl_seconds: int | None = None


class LookupRequest(BaseModel):
    target_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    data_version: str | None = None


class OlapRequest(BaseModel):
    query_kind: str
    params: dict[str, Any] = Field(default_factory=dict)
    data_version: str


class ReconcileRequest(BaseModel):
    total_target_id: str
    group_target_id: str
    policy: str = Field(description=f"可选：{', '.join(POLICY_LABELS)}")


class ClockAdvance(BaseModel):
    seconds: float


def _service() -> WarmupService:
    return warmup_service


@router.get("/meta")
def meta() -> dict[str, Any]:
    """返回数据集版本、查询类型与口径策略等枚举，供前端渲染固定选项。"""
    svc = _service()
    return {
        "dataset": SHARED_DATASET,
        "versions": [VERSION_V1, VERSION_V2],
        "query_kinds": [Q_TOTAL, Q_GROUP],
        "policies": [{"value": k, "label": v} for k, v in POLICY_LABELS.items()],
        "facts": svc._facts,  # 只读展示共享输入数据集内容
    }


@router.get("/overview")
def overview() -> dict[str, Any]:
    svc = _service()
    svc.snapshot_to_store()
    records = svc.list_records()
    batches = svc.list_batches()
    return {
        "targets": len(svc.list_targets()),
        "groups": len(svc.list_groups()),
        "batches": len(batches),
        "records": len(records),
        "valid_records": sum(1 for r in records if r["status"] == "有效"),
        "expired_records": sum(1 for r in records if r["status"] == "已过期"),
        "failed_batches": sum(1 for b in batches if b["status"] != "已完成"),
    }


# -- 缓存目标与分组 ---------------------------------------------------------
@router.get("/targets")
def list_targets() -> dict[str, Any]:
    svc = _service()
    return {"items": svc.list_targets()}


@router.post("/targets", response_model=ActionResult)
def create_target(payload: TargetCreate) -> ActionResult:
    try:
        target = _service().register_target(
            name=payload.name, query_kind=payload.query_kind,
            fixed_params=payload.fixed_params, ttl_seconds=payload.ttl_seconds,
            max_capacity=payload.max_capacity, group_name=payload.group_name)
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message="缓存目标已登记", entry=target)


@router.post("/targets/{target_id}/capacity", response_model=ActionResult)
def update_capacity(target_id: str, payload: CapacityUpdate) -> ActionResult:
    try:
        target = _service().set_capacity(target_id, payload.max_capacity)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=f"目标容量已调整为 {payload.max_capacity}", entry=target)


@router.get("/groups")
def list_groups() -> dict[str, Any]:
    svc = _service()
    groups = []
    for g in svc.list_groups():
        groups.append({
            **g,
            "targets": [svc.get_target(tid) for tid in g["target_ids"]],
        })
    return {"items": groups}


@router.post("/groups/{group_name}/submit")
def submit_group(group_name: str, payload: GroupSubmit) -> dict[str, Any]:
    """同组任务多选范围、一次提交整组，逐条返回已生成版本与暂未纳入目标。"""
    try:
        return _service().submit_group(
            group_name=group_name, target_ids=payload.target_ids,
            data_version=payload.data_version, ttl_seconds=payload.ttl_seconds)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# -- 重建批次包 -------------------------------------------------------------
@router.post("/batches")
def create_batch(payload: BatchCreate) -> dict[str, Any]:
    """一次为多个目标生成缓存重建批次包，按真实 OLAP 结果逐项预热。"""
    try:
        batch = _service().create_batch(
            target_ids=payload.target_ids, data_version=payload.data_version,
            remark=payload.remark, ttl_seconds=payload.ttl_seconds)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _service().snapshot_to_store()
    return batch


@router.get("/batches")
def list_batches() -> dict[str, Any]:
    return {"items": _service().list_batches()}


@router.get("/batches/{batch_id}")
def get_batch(batch_id: str) -> dict[str, Any]:
    try:
        return _service().get_batch(batch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# -- 缓存条目与命中 ---------------------------------------------------------
@router.get("/records")
def list_records(target_id: str | None = None) -> dict[str, Any]:
    return {"items": _service().list_records(target_id)}


@router.post("/lookup")
def lookup(payload: LookupRequest) -> dict[str, Any]:
    """按参数命中缓存：参数范围、数据版本、有效期三道闸门同时满足才算命中。"""
    try:
        return _service().lookup(payload.target_id, payload.params, payload.data_version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/olap")
def run_olap(payload: OlapRequest) -> dict[str, Any]:
    """直接执行真实 OLAP 查询，用于和缓存结果对账。"""
    try:
        return _service().run_olap(payload.query_kind, payload.params, payload.data_version)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# -- 联合查询对账 -----------------------------------------------------------
@router.post("/reconcile")
def reconcile(payload: ReconcileRequest) -> dict[str, Any]:
    try:
        return _service().reconcile(
            total_target_id=payload.total_target_id,
            group_target_id=payload.group_target_id, policy=payload.policy)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/mixed-guard")
def mixed_guard(total_target_id: str, group_target_id: str) -> dict[str, Any]:
    """演示跨版本总计/分组混算被拦截。"""
    return _service().mixed_version_guard(total_target_id, group_target_id)


@router.post("/clock/advance", response_model=ActionResult)
def advance_clock(payload: ClockAdvance) -> ActionResult:
    """测试辅助：推进虚拟时钟以验证 TTL 失效（仅虚拟时钟下可用）。"""
    try:
        _service().advance_clock(payload.seconds)
    except RuntimeError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=f"时钟已推进 {payload.seconds} 秒")


@router.post("/reset", response_model=ActionResult)
def reset_state() -> ActionResult:
    """重置全部预热状态到初始示例口径，便于重复演示与联调。"""
    _service().reset()
    return ActionResult(ok=True, message="预热状态已重置")
