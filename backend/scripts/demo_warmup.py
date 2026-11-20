"""缓存重建批次包端到端场景演示（仅依赖标准库，对运行中的后端发真实 HTTP 请求）。

运行：python3 scripts/demo_warmup.py
它按需求叙事依次验证：
  场景一  v1 多目标批次预热 + 参数/版本/有效期三道命中闸门；
  场景二  更新期间同批次部分预热失败（一项完成、一项超容量，原子不写入）；
  场景三  联合查询两种口径：旧版完整组合 100 vs 新版同批次实时 120，均同版对回；
  场景四  跨版本混算拦截；
  场景五  同组多选整组提交，逐条核对已生成版本与暂未纳入目标；
  场景六  TTL 到期后条目不可命中。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000/api/cache-warmup"


def call(method: str, path: str, payload: dict | None = None, query: str = "") -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    # 路径中可能含中文分组名，按 ASGI 要求做百分号编码
    path = urllib.parse.quote(path, safe="/")
    req = urllib.request.Request(
        f"{BASE}{path}{('?' + query) if query else ''}", data=data,
        headers={"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req) as resp:  # noqa: S310 本地演示地址
        return json.loads(resp.read())


def show(title: str, body: object) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def main() -> None:
    call("POST", "/reset")
    meta = call("GET", "/meta")
    targets = {t["name"]: t for t in call("GET", "/targets")["items"]}
    total_id = targets["销售总额缓存"]["target_id"]
    group_id = targets["区域分组销售缓存"]["target_id"]
    spare_id = targets["品类分组销售缓存（预留）"]["target_id"]
    print(f"共享数据集 {meta['dataset']}，版本 {meta['versions']}，v1 总计 100 / v2 总计 120")

    # 场景一：v1 重建批次包（总计 + 分组两个目标一次提交）
    batch_v1 = call("POST", "/batches", {"target_ids": [total_id, group_id], "data_version": "v1"})
    show("场景一 v1 批次预热（两目标均完成，条目携带版本与有效期）", {
        "batch_id": batch_v1["batch_id"], "status": batch_v1["status"],
        "items": [{"name": i["name"], "status": i["status"], "entries": len(i["records"])}
                  for i in batch_v1["items"]]})

    hit = call("POST", "/lookup", {"target_id": group_id, "params": {"region": "华东"}, "data_version": "v1"})
    miss_param = call("POST", "/lookup", {"target_id": group_id, "params": {"region": "华南"}})
    miss_version = call("POST", "/lookup", {"target_id": group_id,
                                            "params": {"region": "华东"}, "data_version": "v2"})
    show("场景一 命中闸门", {
        "华东@v1 命中": {"hit": hit["hit"], "结果": hit["record"]["result"]},
        "华南（别的参数）": {"hit": miss_param["hit"], "原因": miss_param["reason"]},
        "华东@v2（别的版本）": {"hit": miss_version["hit"], "原因": miss_version["reason"]}})

    # 场景二：更新期间部分失败——分组容量压到 2（需 3 条），v2 批次
    call("POST", f"/targets/{group_id}/capacity", {"max_capacity": 2})
    batch_v2 = call("POST", "/batches", {"target_ids": [total_id, group_id], "data_version": "v2"})
    show("场景二 v2 批次部分预热失败（总计完成 / 分组超容量，失败项零写入）", {
        "batch_status": batch_v2["status"],
        "items": [{"name": i["name"], "status": i["status"], "reason": i["reason"],
                   "entries": len(i["records"])} for i in batch_v2["items"]],
        "分组目标现存条目数": len(call("GET", "/records", query=f"target_id={group_id}")["items"]),
    })
    call("POST", f"/targets/{group_id}/capacity", {"max_capacity": 4})

    # 场景三：两种联合查询口径
    old_combo = call("POST", "/reconcile", {"total_target_id": total_id,
                                            "group_target_id": group_id, "policy": "valid_combo"})
    new_realtime = call("POST", "/reconcile", {"total_target_id": total_id,
                                                "group_target_id": group_id,
                                                "policy": "same_batch_realtime"})
    show("场景三 口径A：继续使用仍有效的完整旧组合", {
        "served_version": old_combo["served_version"], "total": old_combo["total"],
        "group_sum": old_combo["group_sum"], "reconciled": old_combo["reconciled"],
        "sourced_from": old_combo["sourced_from"], "declaration": old_combo["declaration"]})
    show("场景三 口径B：新版同一数据批次实时补缺失分组", {
        "served_version": new_realtime["served_version"], "total": new_realtime["total"],
        "group_sum": new_realtime["group_sum"], "reconciled": new_realtime["reconciled"],
        "realtime_group": new_realtime["realtime_group"],
        "groups": new_realtime["groups"], "declaration": new_realtime["declaration"]})

    # 场景四：跨版本混算拦截
    guard = call("GET", "/mixed-guard", query=f"total_target_id={total_id}&group_target_id={group_id}")
    show("场景四 跨版本总计/分组混算拦截", guard)

    # 场景五：同组多选整组提交（预留目标不勾选 -> 暂未纳入）
    submitted = call("POST", "/groups/区域销售缓存组/submit",
                     {"target_ids": [group_id], "data_version": "v1"})
    show("场景五 同组多选、一次提交、逐条核对", {
        "batch_id": submitted["batch_id"],
        "per_target": [{"name": p["name"], "included": p["included"], "status": p["status"],
                        "generated_version": p["generated_version"],
                        "detail": p.get("detail")} for p in submitted["per_target"]],
        "not_included": submitted["not_included_target_ids"]})
    assert spare_id in submitted["not_included_target_ids"]

    # 场景六：TTL 到期失效（给总计目标单独建一个短 TTL 条目）
    short = call("POST", "/batches", {"target_ids": [total_id], "data_version": "v1", "ttl_seconds": 10})
    rec_id = short["items"][0]["records"][0]["record_id"]
    before = call("POST", "/lookup", {"target_id": total_id, "params": {}})
    call("POST", "/clock/advance", {"seconds": 11})
    after = call("POST", "/lookup", {"target_id": total_id, "params": {}})
    short_check = next(c for c in after["checked"] if c["record_id"] == rec_id)
    show("场景六 TTL 有效期闸门", {
        "短TTL条目": rec_id,
        "到期前该条目可命中": any(c["record_id"] == rec_id and c["hit"] for c in before["checked"]),
        "推进11秒后该条目": short_check,
        "查询整体是否仍命中其它有效条目": after["hit"],
    })

    print("\n全部场景演示完成。")


if __name__ == "__main__":
    main()
