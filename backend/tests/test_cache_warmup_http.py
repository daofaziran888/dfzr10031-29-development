"""HTTP 端到端冒烟测试：不依赖 httpx，直接用 ASGI 协议调用 FastAPI 应用。

重点验证路由接线、请求/响应模型与典型业务链路（预热 -> 命中 -> 对账）在
真实 HTTP 语义下可用；领域规则的细粒度断言在 test_cache_warmup.py 里。
"""
from __future__ import annotations

import asyncio
import json
import unittest

from app.main import app
from app.services.cache_warmup import warmup_service


class AsgiClient:
    """极简 ASGI 测试客户端：够本测试用，不引入第三方依赖。"""

    def __init__(self, asgi_app) -> None:
        self.app = asgi_app

    def request(self, method: str, path: str, payload: dict | None = None,
                query: str = "") -> tuple[int, dict]:
        async def invoke() -> tuple[int, dict]:
            sent_body = json.dumps(payload).encode() if payload is not None else b""
            scope = {
                "type": "http", "http_version": "1.1", "method": method,
                "path": path, "raw_path": path.encode(),
                "query_string": query.encode(),
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(sent_body)).encode())],
                "client": ("testclient", 50000), "server": ("testserver", 80),
                "scheme": "http", "asgi": {"version": "3.0"},
            }
            chunks: list[bytes] = []
            response: dict = {}

            async def receive() -> dict:
                return {"type": "http.request", "body": sent_body, "more_body": False}

            async def send(message: dict) -> None:
                if message["type"] == "http.response.start":
                    response["status"] = message["status"]
                    response["headers"] = dict(message["headers"])
                elif message["type"] == "http.response.body":
                    chunks.append(message.get("body", b""))

            await self.app(scope, receive, send)
            return response["status"], json.loads(b"".join(chunks) or b"null")

        return asyncio.run(invoke())


class HttpSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        warmup_service.reset()
        self.client = AsgiClient(app)
        status, meta = self.client.request("GET", "/api/cache-warmup/meta")
        self.assertEqual(status, 200)
        self.assertEqual(meta["versions"], ["v1", "v2"])
        status, targets = self.client.request("GET", "/api/cache-warmup/targets")
        self.assertEqual(status, 200)
        by_name = {t["name"]: t for t in targets["items"]}
        self.total_id = by_name["销售总额缓存"]["target_id"]
        self.group_id = by_name["区域分组销售缓存"]["target_id"]

    def test_batch_lookup_reconcile_flow_over_http(self) -> None:
        # 1) v1 完整预热
        status, batch = self.client.request("POST", "/api/cache-warmup/batches", {
            "target_ids": [self.total_id, self.group_id], "data_version": "v1"})
        self.assertEqual(status, 200)
        self.assertEqual({i["status"] for i in batch["items"]}, {"已完成"})

        # 2) 命中：参数 + 版本 + 有效期
        status, hit = self.client.request("POST", "/api/cache-warmup/lookup", {
            "target_id": self.group_id, "params": {"region": "华东"}, "data_version": "v1"})
        self.assertEqual(status, 200)
        self.assertTrue(hit["hit"])
        self.assertEqual(hit["record"]["result"]["total"], 60)

        # 3) 越权参数不能命中
        status, miss = self.client.request("POST", "/api/cache-warmup/lookup", {
            "target_id": self.group_id, "params": {"region": "华南"}})
        self.assertEqual(status, 200)
        self.assertFalse(miss["hit"])

        # 4) 分组整组提交（暂未纳入目标可见）
        status, groups = self.client.request("GET", "/api/cache-warmup/groups")
        self.assertEqual(status, 200)
        group = next(g for g in groups["items"] if g["group_name"] == "区域销售缓存组")
        spare = next(t for t in group["targets"] if t["name"] == "品类分组销售缓存（预留）")
        status, submitted = self.client.request(
            "POST", "/api/cache-warmup/groups/区域销售缓存组/submit",
            {"target_ids": [self.group_id], "data_version": "v2"})
        self.assertEqual(status, 200)
        per = {p["target_id"]: p for p in submitted["per_target"]}
        self.assertEqual(per[spare["target_id"]]["status"], "暂未纳入")

        # 5) 联合对账：两种口径都能给出明确版本声明
        for policy, expected_total in (("valid_combo", 100), ("same_batch_realtime", 100)):
            status, report = self.client.request("POST", "/api/cache-warmup/reconcile", {
                "total_target_id": self.total_id,
                "group_target_id": self.group_id, "policy": policy})
            self.assertEqual(status, 200)
            self.assertTrue(report["ok"])
            self.assertEqual(report["total"], expected_total)
            self.assertTrue(report["reconciled"])

    def test_capacity_failure_is_partial_and_atomic(self) -> None:
        status, _ = self.client.request(
            "POST", f"/api/cache-warmup/targets/{self.group_id}/capacity",
            {"max_capacity": 2})
        self.assertEqual(status, 200)
        status, batch = self.client.request("POST", "/api/cache-warmup/batches", {
            "target_ids": [self.total_id, self.group_id], "data_version": "v1"})
        self.assertEqual(status, 200)
        by_id = {i["target_id"]: i for i in batch["items"]}
        self.assertEqual(by_id[self.total_id]["status"], "已完成")
        self.assertEqual(by_id[self.group_id]["status"], "超出容量")
        status, records = self.client.request(
            "GET", "/api/cache-warmup/records", query=f"target_id={self.group_id}")
        self.assertEqual(status, 200)
        self.assertEqual(records["items"], [])

    def test_bad_requests_are_readable(self) -> None:
        status, resp = self.client.request("POST", "/api/cache-warmup/batches", {
            "target_ids": [], "data_version": "v1"})
        self.assertEqual(status, 400)
        self.assertIn("至少选择一个", resp["detail"])
        status, resp = self.client.request("POST", "/api/cache-warmup/reconcile", {
            "total_target_id": "NOPE", "group_target_id": self.group_id,
            "policy": "valid_combo"})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
