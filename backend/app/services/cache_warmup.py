"""缓存重建批次包：按真实 OLAP 查询结果批量预热缓存的核心领域规则。

领域概念
--------
- 共享输入数据集 ``shared_sales``：多个 OLAP 查询（总计、分组）共用同一份事实数据，
  以 ``data_version`` 标识数据版本（v1=更新前，v2=更新后）。
- 缓存目标 ``WarmupTarget``：登记一个目标，绑定 OLAP 查询（总计/分组）、固定参数与
  容量上限。一个目标对应缓存配置里的一类缓存条目。
- 重建批次包 ``RebuildBatch``：一次为多个目标按「真实查询结果」生成预热条目，包里
  每个目标逐项记录完成/失败，失败不影响同包其它目标。
- 缓存条目 ``CacheRecord``：保留实际查询结果、可命中的参数范围、数据版本与有效期；
  命中时必须同时满足参数范围、数据版本、未过期三个条件，别的参数或别的版本都不能命中。
- 联合查询对账：总计与分组只能在同一数据版本内对回，禁止把各数据版本的总计与分组混算。

本模块只依赖标准库与全局 store，时间通过一个可注入的时钟读取，方便测试有效期逻辑。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

from app.store import store

MODULE = "cache_warmup"

# ---------------------------------------------------------------------------
# 共享输入数据集：v1 为更新前数据，v2 为更新后数据（同一份事实表的两个版本）
# 每条事实：区域 region、品类 category、销售额 amount、数据版本
# ---------------------------------------------------------------------------
SHARED_DATASET = "shared_sales"
VERSION_V1 = "v1"
VERSION_V2 = "v2"
# 版本先后顺序：对账时据此识别「最新版」，而不是按字符串字典序猜测
VERSION_ORDER = [VERSION_V1, VERSION_V2]

SEED_FACTS: dict[str, list[dict[str, Any]]] = {
    VERSION_V1: [
        {"region": "华东", "category": "电子", "amount": 40},
        {"region": "华东", "category": "日用", "amount": 20},
        {"region": "华北", "category": "电子", "amount": 25},
        {"region": "华北", "category": "日用", "amount": 15},
    ],
    VERSION_V2: [
        {"region": "华东", "category": "电子", "amount": 45},
        {"region": "华东", "category": "日用", "amount": 25},
        {"region": "华北", "category": "电子", "amount": 30},
        {"region": "华北", "category": "日用", "amount": 20},
    ],
}
# v1 总计 = 100，v2 总计 = 120，与需求给出的对账口径一致。

# 查询类型
Q_TOTAL = "total"   # 总计查询：全表汇总
Q_GROUP = "group"   # 分组查询：按区域分组汇总

# 目标/条目/批次状态
ST_READY = "就绪"
ST_RUNNING = "预热中"
ST_COMPLETED = "已完成"
ST_FAILED_CAPACITY = "超出容量"
ST_PENDING_CAPACITY = "容量不足待补"
ST_NOT_INCLUDED = "暂未纳入"

# 联合查询口径策略
P_VALID_COMBO = "valid_combo"            # 继续使用仍有效的完整旧组合
P_SAME_BATCH_REALTIME = "same_batch_realtime"  # 用新版同一数据批次实时算缺失分组
POLICY_LABELS = {
    P_VALID_COMBO: "继续使用仍有效的完整旧组合",
    P_SAME_BATCH_REALTIME: "以新版同一数据批次实时算出缺失分组",
}


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class WarmupService:
    """缓存预热领域服务：目标/分组登记、批次预热、命中、对账与版本隔离。"""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._lock = threading.RLock()
        self._clock = clock or time.monotonic
        self.reset()

    # -- 测试与初始化 -------------------------------------------------------
    def reset(self) -> None:
        """清空全部预热状态并写回示例目标，回到可演示的初始口径。"""
        with self._lock:
            self._facts: dict[str, list[dict[str, Any]]] = {
                ver: [dict(r) for r in rows] for ver, rows in SEED_FACTS.items()
            }
            self._targets: dict[str, dict[str, Any]] = {}
            self._groups: dict[str, dict[str, Any]] = {}
            self._batches: dict[str, dict[str, Any]] = {}
            self._records: list[dict[str, Any]] = []
            self._ttl_override: dict[str, int] = {}
            self._seed_targets()

    def _seed_targets(self) -> None:
        self.register_target(
            name="销售总额缓存",
            query_kind=Q_TOTAL,
            fixed_params=[],
            ttl_seconds=3600,
            max_capacity=2,
            group_name=None,
        )
        self.register_target(
            name="区域分组销售缓存",
            query_kind=Q_GROUP,
            fixed_params=[{"name": "region", "values": ["ALL", "华东", "华北"]}],
            ttl_seconds=3600,
            max_capacity=4,
            group_name="区域销售缓存组",
        )
        # 登记进同组但不纳入任何提交：用于演示「暂未纳入的目标」
        self.register_target(
            name="品类分组销售缓存（预留）",
            query_kind=Q_GROUP,
            fixed_params=[{"name": "category", "values": ["ALL", "电子", "日用"]}],
            ttl_seconds=3600,
            max_capacity=4,
            group_name="区域销售缓存组",
        )

    # -- 时钟与有效期 -------------------------------------------------------
    def now(self) -> float:
        return self._clock()

    def advance_clock(self, seconds: float) -> None:
        """测试辅助：仅当时钟为可变虚拟时钟时推进时间。"""
        adv = getattr(self._clock, "advance", None)
        if adv is None:
            raise RuntimeError("当前时钟不可推进，请使用虚拟时钟初始化服务")
        adv(seconds)

    # -- OLAP 真实查询（引擎） ----------------------------------------------
    def run_olap(self, query_kind: str, params: dict[str, Any], data_version: str) -> dict[str, Any]:
        """按固定参数在指定数据版本上执行真实 OLAP 查询，返回实际结果。

        引擎只认传入的 data_version，不会跨版本取数，这是「不能混算」的根保证。
        """
        if data_version not in self._facts:
            raise ValueError(f"数据版本 {data_version} 不存在")
        facts = [dict(r) for r in self._facts[data_version]]
        region = params.get("region")
        if region and region != "ALL":
            facts = [r for r in facts if r["region"] == region]
        category = params.get("category")
        if category and category != "ALL":
            facts = [r for r in facts if r["category"] == category]

        total = sum(int(r["amount"]) for r in facts)
        if query_kind == Q_TOTAL:
            return {"query_kind": Q_TOTAL, "data_version": data_version,
                    "params": dict(params), "total": total, "row_count": len(facts)}
        if query_kind == Q_GROUP:
            grouped: dict[str, int] = {}
            for r in facts:
                grouped[r["region"]] = grouped.get(r["region"], 0) + int(r["amount"])
            groups = [{"region": k, "amount": v} for k, v in sorted(grouped.items())]
            return {"query_kind": Q_GROUP, "data_version": data_version,
                    "params": dict(params), "total": total, "groups": groups,
                    "row_count": len(facts)}
        raise ValueError(f"未知查询类型 {query_kind}")

    # -- 目标与分组登记 -----------------------------------------------------
    def register_target(
        self,
        *,
        name: str,
        query_kind: str,
        fixed_params: list[dict[str, Any]],
        ttl_seconds: int,
        max_capacity: int,
        group_name: str | None,
    ) -> dict[str, Any]:
        with self._lock:
            if query_kind not in (Q_TOTAL, Q_GROUP):
                raise ValueError("query_kind 仅支持 total / group")
            if max_capacity <= 0:
                raise ValueError("最大容量必须为正整数")
            target_id = f"TGT-{_new_id()[:8]}"
            target = {
                "target_id": target_id,
                "name": name,
                "query_kind": query_kind,
                "dataset": SHARED_DATASET,
                "fixed_params": [dict(p) for p in fixed_params],
                "ttl_seconds": int(ttl_seconds),
                "max_capacity": int(max_capacity),
                "group_name": group_name,
                "status": ST_READY,
                "current_version": None,
                "created_at": self.now(),
            }
            self._targets[target_id] = target
            if group_name:
                grp = self._groups.setdefault(group_name, {
                    "group_name": group_name, "target_ids": []})
                grp["target_ids"].append(target_id)
            return dict(target)

    def list_targets(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(t) for t in self._targets.values()]

    def get_target(self, target_id: str) -> dict[str, Any] | None:
        with self._lock:
            t = self._targets.get(target_id)
            return dict(t) if t else None

    def list_groups(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(g) for g in self._groups.values()]

    def set_capacity(self, target_id: str, max_capacity: int) -> dict[str, Any]:
        """调整目标容量（用于演示更新期间容量被压缩后预热失败的场景）。"""
        with self._lock:
            target = self._targets.get(target_id)
            if target is None:
                raise KeyError(f"目标 {target_id} 不存在")
            if max_capacity <= 0:
                raise ValueError("最大容量必须为正整数")
            target["max_capacity"] = int(max_capacity)
            return dict(target)

    def set_ttl(self, target_id: str, ttl_seconds: int) -> None:
        with self._lock:
            if target_id not in self._targets:
                raise KeyError(f"目标 {target_id} 不存在")
            self._ttl_override[target_id] = int(ttl_seconds)

    # -- 参数展开：固定参数 -> 具体参数组合与可命中范围 ---------------------
    def _expand_param_combos(self, target: dict[str, Any]) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        """把固定参数定义展开成 (具体参数, 可命中参数范围) 列表。

        固定参数支持两种形态：
        - {"name": "region", "values": ["华东","华北"]}：枚举，每个取值一条，
          可命中范围为 in 谓词；
        - {"name": "amount_min", "range": [0, 100]}：数值区间，整条一条，
          可命中范围为 range 谓词。
        """
        combos: list[dict[str, Any]] = [{}]
        predicates: list[list[dict[str, Any]]] = [[]]
        for spec in target["fixed_params"]:
            name = spec["name"]
            new_combos: list[dict[str, Any]] = []
            new_preds: list[list[dict[str, Any]]] = []
            if "values" in spec:
                for value in spec["values"]:
                    for combo, pred in zip(combos, predicates):
                        c = dict(combo)
                        c[name] = value
                        p = list(pred)
                        # 每个枚举取值都形成精确（eq）可命中范围：ALL 条目只接受
                        # 显式 ALL 或不传该参数的查询，不会吞掉具体取值的查询
                        p.append({"name": name, "op": "eq", "value": value})
                        new_combos.append(c)
                        new_preds.append(p)
            elif "range" in spec:
                low, high = spec["range"]
                for combo, pred in zip(combos, predicates):
                    c = dict(combo)
                    c[name] = low
                    p = list(pred)
                    p.append({"name": name, "op": "range", "low": low, "high": high})
                    new_combos.append(c)
                    new_preds.append(p)
            else:
                raise ValueError(f"固定参数 {name} 必须提供 values 或 range")
            combos, predicates = new_combos, new_preds
        return list(zip(combos, predicates))

    # -- 批次预热 -----------------------------------------------------------
    def create_batch(
        self,
        *,
        target_ids: list[str],
        data_version: str,
        remark: str | None = None,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """一次为多个目标生成缓存重建批次包。

        每个目标独立成功/失败：容量不足的目标整体不写入（原子），标记超出容量；
        其它目标照常完成。返回批次包，逐项可查生成的数据版本与失败原因。
        """
        with self._lock:
            if data_version not in self._facts:
                raise ValueError(f"数据版本 {data_version} 不存在")
            if not target_ids:
                raise ValueError("至少选择一个缓存目标")
            batch_id = f"BAT-{_new_id()[:10]}"
            created_at = self.now()
            batch = {
                "batch_id": batch_id,
                "dataset": SHARED_DATASET,
                "data_version": data_version,
                "remark": remark,
                "created_at": created_at,
                "status": ST_RUNNING,
                "items": [],
            }
            any_failure = False
            for target_id in target_ids:
                target = self._targets.get(target_id)
                if target is None:
                    batch["items"].append({
                        "target_id": target_id, "name": target_id, "status": ST_NOT_INCLUDED,
                        "reason": "目标不存在或已归档", "records": [],
                    })
                    any_failure = True
                    continue
                combos = self._expand_param_combos(target)
                need = len(combos)
                item: dict[str, Any] = {
                    "target_id": target_id,
                    "name": target["name"],
                    "query_kind": target["query_kind"],
                    "planned_entries": need,
                    "max_capacity": target["max_capacity"],
                    "status": ST_RUNNING,
                    "reason": None,
                    "records": [],
                }
                if need > target["max_capacity"]:
                    # 原子失败：一项都不写入，避免半成品缓存被误命中
                    item["status"] = ST_FAILED_CAPACITY
                    item["reason"] = (
                        f"需预热 {need} 条条目，超过目标容量 {target['max_capacity']} 条，整项未写入")
                    target["status"] = ST_FAILED_CAPACITY
                    any_failure = True
                    batch["items"].append(item)
                    continue

                effective_ttl = ttl_seconds if ttl_seconds is not None else self._ttl_override.get(
                    target_id, target["ttl_seconds"])
                record_ids: list[str] = []
                for params, param_range in combos:
                    result = self.run_olap(target["query_kind"], params, data_version)
                    record_id = f"REC-{_new_id()[:10]}"
                    record = {
                        "record_id": record_id,
                        "target_id": target_id,
                        "target_name": target["name"],
                        "batch_id": batch_id,
                        "dataset": SHARED_DATASET,
                        "data_version": data_version,
                        "query_kind": target["query_kind"],
                        "params": params,
                        "param_range": param_range,
                        "result": result,
                        "ttl_seconds": int(effective_ttl),
                        "created_at": created_at,
                        "expires_at": created_at + int(effective_ttl),
                        "status": "有效",
                    }
                    self._records.append(record)
                    record_ids.append(record_id)
                    item["records"].append({
                        "record_id": record_id, "params": dict(params),
                        "param_range": list(param_range),
                    })
                item["status"] = ST_COMPLETED
                target["status"] = ST_COMPLETED
                target["current_version"] = data_version
                target["last_batch_id"] = batch_id
                target["last_warmed_at"] = created_at
                batch["items"].append(item)

            batch["status"] = ST_FAILED_CAPACITY if any_failure else ST_COMPLETED
            self._batches[batch_id] = batch
            return self.get_batch(batch_id)

    def list_batches(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._batch_view(b) for b in self._batches.values()]

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                raise KeyError(f"重建批次包 {batch_id} 不存在")
            return self._batch_view(batch)

    def _batch_view(self, batch: dict[str, Any]) -> dict[str, Any]:
        view = {k: v for k, v in batch.items() if k != "items"}
        view["items"] = [dict(item) for item in batch["items"]]
        view["succeeded_targets"] = [i["target_id"] for i in batch["items"] if i["status"] == ST_COMPLETED]
        view["failed_targets"] = [i["target_id"] for i in batch["items"] if i["status"] != ST_COMPLETED]
        return view

    # -- 缓存条目与命中 -----------------------------------------------------
    def list_records(self, target_id: str | None = None) -> list[dict[str, Any]]:
        """列出缓存条目，附带实时有效性（有效期到点即失效）。"""
        with self._lock:
            now = self.now()
            records = [dict(r) for r in self._records if target_id is None or r["target_id"] == target_id]
            for r in records:
                r["status"] = "有效" if r["expires_at"] > now else "已过期"
            return records

    def _match_range(self, record: dict[str, Any], params: dict[str, Any]) -> bool:
        for pred in record["param_range"]:
            name = pred["name"]
            if pred["op"] == "eq":
                expected = pred["value"]
                if expected == "ALL":
                    # 宽条目只接受「未传该参数」或「显式查 ALL」
                    if name in params and params[name] not in (None, "", "ALL"):
                        return False
                elif params.get(name) != expected:
                    return False
            elif pred["op"] == "in":
                if name not in params or params[name] not in pred["values"]:
                    return False
            elif pred["op"] == "range":
                if name not in params or not (pred["low"] <= params[name] <= pred["high"]):
                    return False
        return True

    def lookup(self, target_id: str, params: dict[str, Any], data_version: str | None = None) -> dict[str, Any]:
        """按参数（与数据版本）命中缓存。

        三道闸门缺一不可：参数范围匹配、数据版本一致、仍在有效期内。
        别的参数、别的版本、过期条目都不能命中；多个条目同时满足时，优先返回
        可命中范围最具体（谓词最多、最新生成）的条目，避免 ALL 宽条目遮蔽精确条目。
        data_version 不传时只按参数匹配，但返回内容仍标明条目版本，由调用方做版本隔离判断。
        """
        with self._lock:
            now = self.now()
            target = self._targets.get(target_id)
            if target is None:
                raise KeyError(f"目标 {target_id} 不存在")
            candidates = [r for r in self._records if r["target_id"] == target_id]
            checked: list[dict[str, Any]] = []
            hits: list[dict[str, Any]] = []
            for r in candidates:
                reason = None
                if not self._match_range(r, params):
                    reason = "参数不在可命中范围"
                elif data_version is not None and r["data_version"] != data_version:
                    reason = f"数据版本不一致（条目 {r['data_version']} / 请求 {data_version}）"
                elif r["expires_at"] <= now:
                    reason = "条目已过有效期"
                checked.append({"record_id": r["record_id"], "data_version": r["data_version"],
                                "expires_at": r["expires_at"], "hit": reason is None, "reason": reason})
                if reason is None:
                    hits.append(r)
            if not hits:
                return {"hit": False, "record": None, "checked": checked,
                        "reason": "没有同时满足参数范围、数据版本与有效期的缓存条目"}
            best = max(hits, key=lambda r: (len(r["param_range"]), r["created_at"]))
            hit = dict(best)
            hit["status"] = "有效"
            return {"hit": True, "record": hit, "checked": checked}

    # -- 分组整组提交：多选范围、一次提交、逐条核对 -------------------------
    def submit_group(
        self,
        *,
        group_name: str,
        target_ids: list[str],
        data_version: str,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """同组任务在缓存入口多选目标，一次提交整组，逐条返回版本与纳入情况。"""
        with self._lock:
            group = self._groups.get(group_name)
            if group is None:
                raise KeyError(f"分组 {group_name} 不存在")
            selected = set(target_ids)
            not_included = [tid for tid in group["target_ids"] if tid not in selected]
            batch = self.create_batch(
                target_ids=[tid for tid in group["target_ids"] if tid in selected],
                data_version=data_version, ttl_seconds=ttl_seconds,
                remark=f"分组「{group_name}」整组提交")
            per_target = []
            for tid in group["target_ids"]:
                target = self._targets[tid]
                if tid in not_included:
                    per_target.append({
                        "target_id": tid, "name": target["name"],
                        "included": False, "status": ST_NOT_INCLUDED,
                        "generated_version": None,
                        "detail": "本次多选未勾选，暂未纳入重建批次",
                    })
                    continue
                item = next((i for i in batch["items"] if i["target_id"] == tid), None)
                per_target.append({
                    "target_id": tid, "name": target["name"], "included": True,
                    "status": item["status"] if item else ST_NOT_INCLUDED,
                    "generated_version": data_version if item and item["status"] == ST_COMPLETED else None,
                    "planned_entries": item["planned_entries"] if item else 0,
                    "reason": item["reason"] if item else None,
                    "record_ids": [r["record_id"] for r in item["records"]] if item else [],
                })
            return {
                "group_name": group_name, "batch_id": batch["batch_id"],
                "data_version": data_version,
                "selected_target_ids": [tid for tid in group["target_ids"] if tid in selected],
                "not_included_target_ids": not_included,
                "per_target": per_target,
                "batch": batch,
            }

    # -- 联合查询对账：同版本对回，禁止跨版本混算 ---------------------------
    @staticmethod
    def _version_rank(version: str) -> int:
        try:
            return VERSION_ORDER.index(version)
        except ValueError:
            return len(VERSION_ORDER)

    def _full_scope_record(self, target_id: str, data_version: str) -> dict[str, Any] | None:
        """取指定版本上全量口径（可命中范围最宽、通常是无参 ALL）的有效条目。"""
        now = self.now()
        candidates = [
            r for r in self._records
            if r["target_id"] == target_id and r["data_version"] == data_version
            and r["expires_at"] > now
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda r: len(r["param_range"]))

    def _latest_record(self, target_id: str, data_version: str | None = None) -> dict[str, Any] | None:
        now = self.now()
        candidates = [
            r for r in self._records
            if r["target_id"] == target_id and r["expires_at"] > now
            and (data_version is None or r["data_version"] == data_version)
        ]
        if not candidates:
            return None
        # 先按数据版本新旧，再按生成时间，保证「最新」判定确定且可解释
        return max(candidates, key=lambda r: (self._version_rank(r["data_version"]), r["created_at"]))

    def reconcile(self, *, total_target_id: str, group_target_id: str, policy: str) -> dict[str, Any]:
        """总计 + 分组联合查询对账。

        口径：
        - ``valid_combo``：优先返回仍在有效期、且总计与分组同属一个版本的完整组合；
          若最新版只有总计、分组缺失，则继续使用旧版完整组合（明确声明读的是更新前结果）。
        - ``same_batch_realtime``：以新版同一数据批次实时补齐缺失分组，与新版总计在
          同一版本对回（明确声明读的是更新后数据）。
        任何情况下都不允许把 v1 分组和 v2 总计（或反过来）拼成一份结果。
        """
        with self._lock:
            if policy not in POLICY_LABELS:
                raise ValueError(f"未知口径策略 {policy}")
            total_target = self._targets.get(total_target_id)
            group_target = self._targets.get(group_target_id)
            if total_target is None or group_target is None:
                raise KeyError("总计或分组目标不存在")
            now = self.now()

            total_latest = self._latest_record(total_target_id)
            group_latest = self._latest_record(group_target_id)
            versions_present = sorted({
                r["data_version"] for r in self._records
                if r["target_id"] in (total_target_id, group_target_id) and r["expires_at"] > now
            })

            base: dict[str, Any] = {
                "policy": policy,
                "policy_label": POLICY_LABELS[policy],
                "dataset": SHARED_DATASET,
                "versions_present": versions_present,
                "total_target": total_target["name"],
                "group_target": group_target["name"],
                "mixed_version_blocked": False,
            }

            # 先找是否存在同版本完整组合（总计与分组都在有效期内且版本一致）
            def combo(version: str) -> dict[str, Any] | None:
                t = self._full_scope_record(total_target_id, version)
                g = self._full_scope_record(group_target_id, version)
                if t and g:
                    return {"version": version, "total_record": t, "group_record": g}
                return None

            same_version_combos = [c for c in (combo(v) for v in versions_present) if c]

            if policy == P_VALID_COMBO:
                if not same_version_combos:
                    return {**base, "ok": False,
                            "reason": "不存在仍有效的同版本完整组合，无法对账（也不允许跨版本混算）"}
                # 选择版本最新的完整组合；若最新版不完整，自然落到旧版完整组合
                chosen = max(same_version_combos, key=lambda c: self._version_rank(c["version"]))
                t, g = chosen["total_record"], chosen["group_record"]
                group_sum = sum(item["amount"] for item in g["result"]["groups"])
                latest_version = total_latest["data_version"] if total_latest else None
                is_fallback = chosen["version"] != latest_version
                return {
                    **base, "ok": True, "served_version": chosen["version"],
                    "sourced_from": "更新前的完整缓存组合（新版仅总计完成，分组缺失，回退旧组合）"
                    if is_fallback else "同版本完整缓存组合",
                    "fell_back_to_old": is_fallback,
                    "total": t["result"]["total"],
                    "groups": g["result"]["groups"],
                    "group_sum": group_sum,
                    "reconciled": group_sum == t["result"]["total"],
                    "total_record_id": t["record_id"],
                    "group_record_id": g["record_id"],
                    "declaration": f"本次联合查询读取的是数据版本 {chosen['version']} 的完整结果，总计与分组同版本对回",
                }

            # same_batch_realtime：以最新版总计为准，分组缺失则用同一数据批次实时计算
            if total_latest is None:
                return {**base, "ok": False, "reason": "没有任何有效的总计缓存条目，无法确定服务版本"}
            version = total_latest["data_version"]
            g = self._full_scope_record(group_target_id, version)
            realtime = False
            if g is None:
                # 关键：实时计算必须使用与总计相同的数据版本，杜绝跨版本混算
                g_result = self.run_olap(Q_GROUP, {}, version)
                g = {"record_id": None, "data_version": version, "result": g_result,
                     "expires_at": None, "param_range": []}
                realtime = True
            group_sum = sum(item["amount"] for item in g["result"]["groups"])
            return {
                **base, "ok": True, "served_version": version,
                "sourced_from": "新版同一数据批次实时计算缺失分组" if realtime else "新版同版本缓存组合",
                "total": total_latest["result"]["total"],
                "groups": g["result"]["groups"],
                "group_sum": group_sum,
                "reconciled": group_sum == total_latest["result"]["total"],
                "total_record_id": total_latest["record_id"],
                "group_record_id": g["record_id"],
                "realtime_group": realtime,
                "declaration": f"本次联合查询读取的是数据版本 {version}（更新后数据），缺失分组由同批次实时补齐，未跨版本混算",
            }

    def mixed_version_guard(self, total_target_id: str, group_target_id: str) -> dict[str, Any]:
        """显式演示跨版本混算被拦截：v1 总计 + v2 分组（或反过来）一律拒绝。"""
        with self._lock:
            now = self.now()
            valid = [r for r in self._records if r["expires_at"] > now]

            def full_scope(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
                # 每个版本取全量口径（可命中范围最宽、无参）的条目做代表
                scoped = [r for r in rows]
                if not scoped:
                    return None
                return min(scoped, key=lambda r: len(r["param_range"]))

            totals_rows: dict[str, list[dict[str, Any]]] = {}
            groups_rows: dict[str, list[dict[str, Any]]] = {}
            for r in valid:
                if r["target_id"] == total_target_id:
                    totals_rows.setdefault(r["data_version"], []).append(r)
                elif r["target_id"] == group_target_id:
                    groups_rows.setdefault(r["data_version"], []).append(r)
            totals = {v: full_scope(rows) for v, rows in totals_rows.items()}
            groups = {v: full_scope(rows) for v, rows in groups_rows.items()}
            attempts = []
            for tv, tr in totals.items():
                for gv, gr in groups.items():
                    if tv == gv or tr is None or gr is None:
                        continue
                    attempts.append({
                        "total_version": tv, "group_version": gv,
                        "blocked": True,
                        "reason": f"总计版本 {tv} 与分组版本 {gv} 不一致，禁止混算",
                        "would_total": tr["result"]["total"],
                        "would_group_sum": sum(x["amount"] for x in gr["result"]["groups"]),
                    })
            return {"blocked_attempts": attempts,
                    "rule": "联合查询只允许同一数据版本内的总计与分组对账"}

    # -- 持久化到全局 store（供 overview 等通用接口读取） -------------------
    def snapshot_to_store(self) -> None:
        with self._lock:
            store.rows(MODULE)  # 确保表存在
            store._tables[MODULE] = [  # type: ignore[attr-defined]
                {"id": idx + 1, "status": r["status"], "pending": False, "abnormal": r["status"] == "已过期",
                 "缓存编号": r["record_id"], "缓存目标": r["target_name"],
                 "缓存策略": r["query_kind"], "过期秒数": r["ttl_seconds"],
                 "最大容量": self._targets[r["target_id"]]["max_capacity"],
                 "命中统计": f"版本 {r['data_version']}",
                 "淘汰策略": "TTL 过期", "缓存状态": r["status"]}
                for idx, r in enumerate(self.list_records())
            ]


class VirtualClock:
    """测试用虚拟时钟：可自由推进，模拟 TTL 到期。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class OffsetMonotonicClock:
    """运行期时钟：以真实单调时间为基准，叠加可推进的偏移量。

    既保证线上 TTL 随真实时间自然到期，又允许通过 advance() 演示/验证失效逻辑。
    """

    def __init__(self) -> None:
        self._offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self._offset

    def advance(self, seconds: float) -> None:
        self._offset += float(seconds)


# 进程级单例：路由层共享同一份预热状态
warmup_service = WarmupService(clock=OffsetMonotonicClock())
