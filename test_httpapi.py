"""HTTP API 端到端测试：REST 接口串联完整业务，并保持 /health 稳定契约。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from freshkeep.clock import SimClock
from freshkeep.httpapi import ApiState, make_handler


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = ApiState(clock=SimClock("2026-05-01T00:00:00Z"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.state))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method, path, body=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = Request(self.base + path, data=data, method=method,
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def setUp(self):
        self.call("POST", "/v1/admin/reset")
        self.state.clock.set("2026-05-01T00:00:00Z")

    def _setup_lineage(self):
        self.call("POST", "/v1/rules/versions", {
            "version_id": "R1", "effective_from": "2026-01-01T00:00:00Z",
            "note": "基线"})
        self.call("POST", "/v1/lineage/seed-batches",
                  {"batch_id": "SB1", "cultivar": "卡罗拉玫瑰"})
        self.call("POST", "/v1/lineage/greenhouses",
                  {"house_id": "GH1", "grower_id": "G1"})
        self.call("POST", "/v1/lineage/shifts",
                  {"shift_id": "SH1", "house_id": "GH1",
                   "cut_at": "2026-04-30T20:00:00Z", "maturity_stage": 3})
        self.call("POST", "/v1/lineage/post-harvest", {
            "record_id": "PH1", "shift_id": "SH1",
            "precool_started_at": "2026-04-30T20:30:00Z",
            "precool_ended_at": "2026-04-30T22:30:00Z"})
        for bid in ("B1", "B2"):
            self.call("POST", "/v1/lineage/bouquets", {
                "bouquet_id": bid, "seed_batch_id": "SB1", "house_id": "GH1",
                "shift_id": "SH1", "post_harvest_id": "PH1"})
        self.call("POST", "/v1/lineage/containers",
                  {"container_id": "BOX1", "kind": "box"})
        self.call("POST", "/v1/lineage/containers",
                  {"container_id": "V1", "kind": "vehicle"})
        self.call("POST", "/v1/lineage/events", {
            "event_id": "E1", "type": "pack", "container_id": "BOX1",
            "bouquet_ids": ["B1", "B2"], "at": "2026-04-30T23:00:00Z"})
        self.call("POST", "/v1/lineage/events", {
            "event_id": "E2", "type": "load", "container_id": "BOX1",
            "vehicle_id": "V1", "at": "2026-05-01T01:00:00Z"})
        self.call("POST", "/v1/lineage/loggers",
                  {"logger_id": "L1", "container_id": "BOX1"})

    def test_full_flow_over_http(self):
        self._setup_lineage()
        # 重复事件幂等
        _, dup = self.call("POST", "/v1/lineage/events", {
            "event_id": "E1", "type": "pack", "container_id": "BOX1",
            "bouquet_ids": ["B1", "B2"], "at": "2026-04-30T23:00:00Z"})
        self.assertTrue(dup["deduplicated"])

        status, order = self.call("POST", "/v1/orders", {
            "order_id": "O1",
            "lines": [{"line_id": "L1", "bouquet_ids": ["B1", "B2"],
                       "channel": "wedding", "unit_price": 100,
                       "planned_transit_hours": 40}],
            "placed_at": "2026-05-01T00:00:00Z", "customer": "婚庆甲"})
        self.assertEqual(status, 200)
        self.assertEqual(order["rule_version_id"], "R1")
        self.assertEqual(order["lines"][0]["promise"]["status"], "promised")

        # 温度：正常 → 超温一小时 → 恢复
        for seq, (hm, temp) in enumerate([
                ("02:00", 3.0), ("03:00", 14.0), ("04:00", 14.0),
                ("05:00", 3.0), ("12:00", 3.0)], start=1):
            s, _ = self.call("POST", "/v1/temperature/readings", {
                "logger_id": "L1", "seq": seq,
                "read_at": f"2026-05-01T{hm}:00Z", "temp_c": temp})
            self.assertEqual(s, 200)
        # 承运回调重复
        self.call("POST", "/v1/orders/O1/carrier-events", {
            "event_id": "C1", "event_type": "departed",
            "at": "2026-05-01T01:00:00Z", "vehicle_id": "V1"})
        _, cdup = self.call("POST", "/v1/orders/O1/carrier-events", {
            "event_id": "C1", "event_type": "departed",
            "at": "2026-05-01T01:00:00Z", "vehicle_id": "V1"})
        self.assertTrue(cdup["deduplicated"])

        # 分批签收
        self.state.clock.set("2026-05-03T00:00:00Z")
        s, r1 = self.call("POST", "/v1/orders/O1/receipts", {
            "receipt_id": "RC1", "line_id": "L1", "bouquet_ids": ["B1"],
            "at": "2026-05-02T16:00:00Z"})
        self.assertFalse(r1["fully_received"])
        s, r2 = self.call("POST", "/v1/orders/O1/receipts", {
            "receipt_id": "RC2", "line_id": "L1", "bouquet_ids": ["B2"],
            "at": "2026-05-02T17:00:00Z"})
        self.assertTrue(r2["fully_received"])

        _, incidents = self.call("GET", "/v1/incidents")
        temp_ids = [i["incident_id"] for i in incidents["incidents"]
                    if i["type"] == "temperature_excursion"]
        self.assertEqual(len(temp_ids), 1)
        iid = temp_ids[0]

        # 未补证前结算被门控
        status, err = self.call("POST", "/v1/orders/O1/lines/L1/settle", {})
        self.assertEqual(status, 400)
        self.assertIn("补证", err["error"])

        self.call("POST", f"/v1/incidents/{iid}/evidence", {
            "party": "carrier", "explanation": "冷机故障",
            "evidence_ref": "EV1"})
        self.call("POST", f"/v1/incidents/{iid}/adjudicate", {
            "liability": "carrier", "note": "承运责任"})
        status, settle = self.call("POST", "/v1/orders/O1/lines/L1/settle", {})
        self.assertEqual(status, 200)
        self.assertEqual(settle["settlement"]["customer_refund"], 100.0)

        # 客服与管理层视图可用
        s, cs = self.call("GET", "/v1/views/customer-service/O1")
        self.assertEqual(s, 200)
        self.assertEqual(cs["lines"][0]["compensation"]["customer_refund_total"], 100.0)
        s, replay = self.call("GET", "/v1/views/replay/O1")
        self.assertEqual(s, 200)
        self.assertIn("timeline", replay)
        s, grower = self.call("GET", "/v1/views/grower/G1")
        self.assertEqual(s, 200)
        self.assertEqual(grower["settlement_count"], 1)

    def test_gap_backfill_and_idempotent_receipts_over_http(self):
        self._setup_lineage()
        self.call("POST", "/v1/orders", {
            "order_id": "O2",
            "lines": [{"line_id": "L1", "bouquet_ids": ["B1", "B2"],
                       "channel": "florist", "unit_price": 80}],
            "placed_at": "2026-05-01T00:00:00Z"})
        self.call("POST", "/v1/temperature/readings", {
            "logger_id": "L1", "seq": 1,
            "read_at": "2026-05-01T02:00:00Z", "temp_c": 3.0})
        self.call("POST", "/v1/temperature/gaps", {
            "logger_id": "L1", "gap_id": "G1",
            "disconnected_at": "2026-05-01T02:00:00Z",
            "resumed_at": "2026-05-01T06:00:00Z", "reason": "断电"})
        _, bf = self.call("POST", "/v1/temperature/gaps/backfill", {
            "logger_id": "L1", "gap_id": "G1", "event_id": "BF1",
            "readings": [
                {"seq": 2, "read_at": "2026-05-01T03:00:00Z", "temp_c": 3.0},
                {"seq": 3, "read_at": "2026-05-01T04:00:00Z", "temp_c": 3.0}]})
        self.assertEqual(bf["inserted"], 2)
        self.state.clock.set("2026-05-03T00:00:00Z")
        self.call("POST", "/v1/orders/O2/receipts", {
            "receipt_id": "RC1", "line_id": "L1", "bouquet_ids": ["B1", "B2"],
            "at": "2026-05-02T12:00:00Z"})
        status, settle = self.call("POST", "/v1/orders/O2/lines/L1/settle", {})
        self.assertEqual(status, 200)
        self.assertEqual(settle["settlement"]["customer_refund"], 0.0)

    def test_error_routes(self):
        status, body = self.call("GET", "/v1/orders/NOPE")
        self.assertEqual(status, 400)
        status, body = self.call("GET", "/nope")
        self.assertEqual(status, 404)
        status, body = self.call("POST", "/v1/orders", {"order_id": "X"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
