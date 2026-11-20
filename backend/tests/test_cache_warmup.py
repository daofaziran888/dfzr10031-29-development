"""缓存重建批次包领域规则测试。

覆盖需求中的每条约束：
1. 一次为多个目标生成重建批次包，按真实 OLAP 查询结果预热；
2. 条目保留实际结果、可命中参数范围、数据版本与有效期，别的参数/版本不能命中；
3. 同一批次部分预热失败：一项完成、一项超容量（原子不写入）；
4. 联合查询对账：声明读的是更新前完整结果还是更新后数据，禁止跨版本混算；
5. 同组多选、一次提交，逐条核对已生成版本与暂未纳入目标；
6. 只有新版总计完成时，两种口径策略的选择与 100/120 对账结果；
7. TTL 过期后条目不可命中；数值区间参数范围的命中语义。
"""
from __future__ import annotations

import unittest

from app.services.cache_warmup import (
    P_SAME_BATCH_REALTIME,
    P_VALID_COMBO,
    Q_GROUP,
    Q_TOTAL,
    ST_COMPLETED,
    ST_FAILED_CAPACITY,
    ST_NOT_INCLUDED,
    VERSION_V1,
    VERSION_V2,
    VirtualClock,
    WarmupService,
)


class WarmupTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = VirtualClock()
        self.svc = WarmupService(clock=self.clock)
        targets = {t["name"]: t for t in self.svc.list_targets()}
        self.total_t = targets["销售总额缓存"]
        self.group_t = targets["区域分组销售缓存"]
        self.spare_t = targets["品类分组销售缓存（预留）"]
        self.total_id = self.total_t["target_id"]
        self.group_id = self.group_t["target_id"]
        self.spare_id = self.spare_t["target_id"]


class TestOlapEngine(WarmupTestBase):
    def test_shared_dataset_two_versions_totals_100_and_120(self) -> None:
        v1 = self.svc.run_olap(Q_TOTAL, {}, VERSION_V1)
        v2 = self.svc.run_olap(Q_TOTAL, {}, VERSION_V2)
        self.assertEqual(v1["total"], 100)  # 更新前总计 100
        self.assertEqual(v2["total"], 120)  # 更新后总计 120

    def test_group_query_sums_back_to_total_same_version(self) -> None:
        for version, expected in ((VERSION_V1, 100), (VERSION_V2, 120)):
            g = self.svc.run_olap(Q_GROUP, {}, version)
            self.assertEqual(sum(x["amount"] for x in g["groups"]), expected)
            self.assertEqual(g["data_version"], version)

    def test_group_query_with_region_param(self) -> None:
        g = self.svc.run_olap(Q_GROUP, {"region": "华东"}, VERSION_V1)
        self.assertEqual(g["total"], 60)
        self.assertEqual([x["region"] for x in g["groups"]], ["华东"])

    def test_engine_never_crosses_versions(self) -> None:
        # 引擎按显式版本取数，v2 的结果绝不可能出现在 v1 查询里
        self.assertNotEqual(
            self.svc.run_olap(Q_TOTAL, {}, VERSION_V1)["total"],
            self.svc.run_olap(Q_TOTAL, {}, VERSION_V2)["total"],
        )


class TestBatchWarmup(WarmupTestBase):
    def test_batch_warms_multiple_targets_with_real_results(self) -> None:
        batch = self.svc.create_batch(
            target_ids=[self.total_id, self.group_id], data_version=VERSION_V1)
        self.assertEqual(batch["data_version"], VERSION_V1)
        statuses = {i["target_id"]: i["status"] for i in batch["items"]}
        self.assertEqual(statuses[self.total_id], ST_COMPLETED)
        self.assertEqual(statuses[self.group_id], ST_COMPLETED)
        # 总计 1 条 + 分组 3 条（ALL/华东/华北）
        records = self.svc.list_records()
        self.assertEqual(len(records), 4)
        total_records = self.svc.list_records(self.total_id)
        self.assertEqual(total_records[0]["result"]["total"], 100)
        # 条目可追溯数据版本与有效期
        for r in records:
            self.assertEqual(r["data_version"], VERSION_V1)
            self.assertGreater(r["expires_at"], r["created_at"])
            self.assertEqual(r["ttl_seconds"], 3600)
            self.assertTrue(r["param_range"] is not None)

    def test_partial_failure_one_completed_one_over_capacity(self) -> None:
        # 分组目标需 3 条，容量压到 2：整项原子失败，不写入任何条目
        self.svc.set_capacity(self.group_id, 2)
        batch = self.svc.create_batch(
            target_ids=[self.total_id, self.group_id], data_version=VERSION_V1)
        by_id = {i["target_id"]: i for i in batch["items"]}
        self.assertEqual(by_id[self.total_id]["status"], ST_COMPLETED)
        self.assertEqual(by_id[self.group_id]["status"], ST_FAILED_CAPACITY)
        self.assertIn("超过目标容量", by_id[self.group_id]["reason"])
        # 失败项目没有产生任何半成品条目；成功项目的条目照常保留
        self.assertEqual(len(self.svc.list_records(self.group_id)), 0)
        self.assertEqual(len(self.svc.list_records(self.total_id)), 1)
        self.assertEqual(batch["succeeded_targets"], [self.total_id])
        self.assertEqual(batch["failed_targets"], [self.group_id])
        # 批次包整体标记为存在失败，但成功项的数据版本仍可逐项查到
        succeeded_item = by_id[self.total_id]
        self.assertTrue(succeeded_item["records"])

    def test_lookup_requires_param_range_version_and_ttl(self) -> None:
        self.svc.create_batch(
            target_ids=[self.total_id, self.group_id], data_version=VERSION_V1)
        # 精确参数优先于 ALL 宽条目
        hit = self.svc.lookup(self.group_id, {"region": "华东"})
        self.assertTrue(hit["hit"])
        self.assertEqual(hit["record"]["params"]["region"], "华东")
        self.assertEqual(hit["record"]["result"]["total"], 60)
        # ALL 条目不约束 region，任何 region 都能命中
        hit_all = self.svc.lookup(self.group_id, {"region": "ALL"})
        self.assertTrue(hit_all["hit"])
        self.assertEqual(hit_all["record"]["result"]["total"], 100)
        # 别的参数不能命中：枚举里没有「华南」
        miss = self.svc.lookup(self.group_id, {"region": "华南"})
        self.assertFalse(miss["hit"])
        self.assertTrue(all(c["reason"] == "参数不在可命中范围" for c in miss["checked"]))
        # 别的版本不能命中 v1 条目
        miss_version = self.svc.lookup(self.group_id, {"region": "华东"}, data_version=VERSION_V2)
        self.assertFalse(miss_version["hit"])
        self.assertTrue(any("数据版本不一致" in c["reason"] for c in miss_version["checked"]))

    def test_expired_record_cannot_be_hit(self) -> None:
        self.svc.create_batch(target_ids=[self.total_id], data_version=VERSION_V1,
                              ttl_seconds=10)
        self.assertTrue(self.svc.lookup(self.total_id, {})["hit"])
        self.clock.advance(11)
        miss = self.svc.lookup(self.total_id, {})
        self.assertFalse(miss["hit"])
        self.assertTrue(all(c["reason"] == "条目已过有效期" for c in miss["checked"]))
        # 列表里同样反映过期状态
        self.assertEqual(self.svc.list_records(self.total_id)[0]["status"], "已过期")

    def test_numeric_range_param_predicate(self) -> None:
        target = self.svc.register_target(
            name="区间参数缓存", query_kind=Q_TOTAL,
            fixed_params=[{"name": "amount_min", "range": [0, 100]}],
            ttl_seconds=600, max_capacity=1, group_name=None)
        self.svc.create_batch(target_ids=[target["target_id"]], data_version=VERSION_V1)
        self.assertTrue(self.svc.lookup(target["target_id"], {"amount_min": 50})["hit"])
        self.assertFalse(self.svc.lookup(target["target_id"], {"amount_min": 150})["hit"])


class TestGroupSubmit(WarmupTestBase):
    def test_group_multi_select_one_submit_per_target_status(self) -> None:
        # 同组两个目标，只多选分组目标，品类目标暂未纳入
        result = self.svc.submit_group(
            group_name="区域销售缓存组",
            target_ids=[self.group_id], data_version=VERSION_V1)
        per = {p["target_id"]: p for p in result["per_target"]}
        self.assertTrue(per[self.group_id]["included"])
        self.assertEqual(per[self.group_id]["status"], ST_COMPLETED)
        self.assertEqual(per[self.group_id]["generated_version"], VERSION_V1)
        self.assertFalse(per[self.spare_id]["included"])
        self.assertEqual(per[self.spare_id]["status"], ST_NOT_INCLUDED)
        self.assertIsNone(per[self.spare_id]["generated_version"])
        self.assertEqual(result["not_included_target_ids"], [self.spare_id])
        # 整组一次提交只产生一个批次包
        self.assertEqual(len(self.svc.list_batches()), 1)

    def test_group_submit_reports_capacity_failure_per_target(self) -> None:
        self.svc.set_capacity(self.group_id, 1)
        result = self.svc.submit_group(
            group_name="区域销售缓存组",
            target_ids=[self.group_id, self.spare_id], data_version=VERSION_V1)
        per = {p["target_id"]: p for p in result["per_target"]}
        self.assertEqual(per[self.group_id]["status"], ST_FAILED_CAPACITY)
        self.assertIsNone(per[self.group_id]["generated_version"])
        self.assertEqual(per[self.spare_id]["status"], ST_COMPLETED)
        self.assertEqual(per[self.spare_id]["generated_version"], VERSION_V1)


class TestReconcile(WarmupTestBase):
    def _warm_v1_combo(self, ttl_seconds: int | None = None) -> None:
        self.svc.create_batch(
            target_ids=[self.total_id, self.group_id], data_version=VERSION_V1,
            ttl_seconds=ttl_seconds)

    def _warm_v2_total_only(self, ttl_seconds: int | None = None) -> None:
        # 模拟「只有新版总计完成预热」：分组目标在 v2 批次中容量不足
        self.svc.set_capacity(self.group_id, 2)
        batch = self.svc.create_batch(
            target_ids=[self.total_id, self.group_id], data_version=VERSION_V2,
            ttl_seconds=ttl_seconds)
        by_id = {i["target_id"]: i for i in batch["items"]}
        self.assertEqual(by_id[self.total_id]["status"], ST_COMPLETED)
        self.assertEqual(by_id[self.group_id]["status"], ST_FAILED_CAPACITY)
        # 恢复容量，不影响已生成的条目状态
        self.svc.set_capacity(self.group_id, 4)

    def test_same_version_combo_reconciles_v1(self) -> None:
        self._warm_v1_combo()
        report = self.svc.reconcile(
            total_target_id=self.total_id, group_target_id=self.group_id,
            policy=P_VALID_COMBO)
        self.assertTrue(report["ok"])
        self.assertEqual(report["served_version"], VERSION_V1)
        self.assertEqual(report["total"], 100)
        self.assertEqual(report["group_sum"], 100)
        self.assertTrue(report["reconciled"])

    def test_partial_new_version_valid_combo_serves_old_complete_set(self) -> None:
        """旧组合仍有效、新版只有总计：valid_combo 返回更新前完整结果（总计 100）。"""
        self._warm_v1_combo()
        self._warm_v2_total_only()
        report = self.svc.reconcile(
            total_target_id=self.total_id, group_target_id=self.group_id,
            policy=P_VALID_COMBO)
        self.assertTrue(report["ok"])
        self.assertEqual(report["served_version"], VERSION_V1)
        self.assertEqual(report["total"], 100)  # 明确返回旧版 100，而不是新版 120
        self.assertEqual(report["group_sum"], 100)
        self.assertTrue(report["reconciled"])
        self.assertIn("更新前", report["sourced_from"])
        self.assertIn(VERSION_V1, report["declaration"])

    def test_partial_new_version_realtime_serves_new_version_120(self) -> None:
        """same_batch_realtime：用新版同一数据批次实时补分组，返回 120 且同版对回。"""
        self._warm_v1_combo()
        self._warm_v2_total_only()
        report = self.svc.reconcile(
            total_target_id=self.total_id, group_target_id=self.group_id,
            policy=P_SAME_BATCH_REALTIME)
        self.assertTrue(report["ok"])
        self.assertEqual(report["served_version"], VERSION_V2)
        self.assertTrue(report["realtime_group"])
        self.assertEqual(report["total"], 120)
        self.assertEqual(report["group_sum"], 120)
        self.assertTrue(report["reconciled"])
        # 实时分组与直接跑 v2 OLAP 的结果完全一致
        rt = self.svc.run_olap(Q_GROUP, {}, VERSION_V2)
        self.assertEqual(report["groups"], rt["groups"])
        self.assertIn("更新后", report["declaration"])

    def test_realtime_missing_group_uses_same_batch_not_old_cache(self) -> None:
        self._warm_v1_combo()
        self._warm_v2_total_only()
        report = self.svc.reconcile(
            total_target_id=self.total_id, group_target_id=self.group_id,
            policy=P_SAME_BATCH_REALTIME)
        # 分组金额必须是 v2 口径（华东 70、华北 50），不能取 v1 缓存（60/40）
        groups = {g["region"]: g["amount"] for g in report["groups"]}
        self.assertEqual(groups, {"华东": 70, "华北": 50})

    def test_mixed_version_totals_and_groups_are_blocked(self) -> None:
        self._warm_v1_combo()
        self._warm_v2_total_only()
        guard = self.svc.mixed_version_guard(self.total_id, self.group_id)
        self.assertTrue(guard["blocked_attempts"])
        for attempt in guard["blocked_attempts"]:
            self.assertTrue(attempt["blocked"])
            self.assertNotEqual(attempt["total_version"], attempt["group_version"])
            # 取全量口径条目：v2 总计 120 与 v1 分组求和 100 若混算必然对不上
            self.assertEqual(attempt["would_total"], 120)
            self.assertEqual(attempt["would_group_sum"], 100)

    def test_valid_combo_after_old_set_expires_falls_back_to_realtime_path(self) -> None:
        # 旧组合 TTL 较短，新版总计 TTL 较长：旧组合到期而新版总计仍有效
        self._warm_v1_combo(ttl_seconds=1000)
        self._warm_v2_total_only(ttl_seconds=7200)
        self.clock.advance(2000)
        report = self.svc.reconcile(
            total_target_id=self.total_id, group_target_id=self.group_id,
            policy=P_VALID_COMBO)
        self.assertFalse(report["ok"])
        self.assertIn("跨版本", report["reason"])
        # 实时口径仍可工作：只剩 v2 总计有效，分组实时补齐
        rt_report = self.svc.reconcile(
            total_target_id=self.total_id, group_target_id=self.group_id,
            policy=P_SAME_BATCH_REALTIME)
        self.assertTrue(rt_report["ok"])
        self.assertEqual(rt_report["served_version"], VERSION_V2)
        self.assertTrue(rt_report["reconciled"])

    def test_full_v2_combo_reconciles_at_120(self) -> None:
        self._warm_v1_combo()
        self.svc.create_batch(
            target_ids=[self.total_id, self.group_id], data_version=VERSION_V2)
        for policy in (P_VALID_COMBO, P_SAME_BATCH_REALTIME):
            report = self.svc.reconcile(
                total_target_id=self.total_id, group_target_id=self.group_id, policy=policy)
            self.assertTrue(report["ok"])
            self.assertEqual(report["served_version"], VERSION_V2)
            self.assertEqual(report["total"], 120)
            self.assertEqual(report["group_sum"], 120)
            self.assertTrue(report["reconciled"])


class TestBatchTraceability(WarmupTestBase):
    def test_records_trace_batch_version_and_validity(self) -> None:
        batch = self.svc.create_batch(
            target_ids=[self.total_id], data_version=VERSION_V1)
        record = self.svc.list_records(self.total_id)[0]
        self.assertEqual(record["batch_id"], batch["batch_id"])
        self.assertEqual(record["data_version"], VERSION_V1)
        self.assertEqual(record["dataset"], "shared_sales")
        self.assertEqual(record["expires_at"] - record["created_at"], record["ttl_seconds"])
        # 批次详情可反查到条目
        detail = self.svc.get_batch(batch["batch_id"])
        self.assertEqual(detail["items"][0]["records"][0]["record_id"], record["record_id"])


if __name__ == "__main__":
    unittest.main()
