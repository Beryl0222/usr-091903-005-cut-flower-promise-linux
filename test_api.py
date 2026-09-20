"""HTTP JSON API 端到端测试：在真实端口上验证路由、幂等与错误码。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from service import make_handler


def post(url, data):
    request = Request(
        url, data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def get(url):
    try:
        with urlopen(url, timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


class ApiFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler())
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/api"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_full_order_lifecycle_over_http(self):
        T = "2026-09-05T08:00:00"
        post(f"{self.base}/seed-batches", {"seed_batch_id": "S1", "cultivar": "卡罗拉", "at": T})
        post(f"{self.base}/greenhouses", {"greenhouse_id": "G1", "name": "一号棚", "at": T})
        post(f"{self.base}/cut-shifts", {"shift_id": "SH1", "greenhouse_id": "G1",
                                         "seed_batch_id": "S1", "at": T})
        status, body = post(f"{self.base}/bouquets/harvest",
                            {"shift_id": "SH1", "bouquet_ids": ["F1", "F2"], "at": T})
        self.assertEqual(status, 200)
        post(f"{self.base}/postharvest", {"bouquet_ids": ["F1", "F2"],
                                          "precool_wait_min": 10, "at": T})
        post(f"{self.base}/boxes/pack", {"box_id": "BX1", "bouquet_ids": ["F1", "F2"],
                                         "logger_id": "L1", "at": T})
        dep = "2026-09-05T10:00:00"
        post(f"{self.base}/shipments", {"shipment_id": "TR1", "carrier": "云冷链",
                                        "box_ids": ["BX1"], "vehicle_id": "V1",
                                        "vehicle_logger_id": "VL1", "at": dep})

        # 报价 -> 可承诺
        status, quote = post(f"{self.base}/orders/quote",
                             {"bouquet_ids": ["F1", "F2"], "at": dep})
        self.assertEqual(status, 200)
        self.assertTrue(quote["committable"])
        self.assertEqual(quote["rule_version"], "rule-v1-2026-08")

        # 接单（带幂等键，重放同键被拒）
        status, order = post(f"{self.base}/orders",
                             {"order_id": "O1", "bouquet_ids": ["F1", "F2"], "amount": 200,
                              "idempotency_key": "accept-1", "at": dep})
        self.assertEqual(status, 200)
        status, dup = post(f"{self.base}/orders",
                           {"order_id": "O1", "bouquet_ids": ["F1", "F2"], "amount": 200,
                            "idempotency_key": "accept-1", "at": dep})
        self.assertEqual(status, 409)
        self.assertEqual(dup["error"], "conflict")

        # 温度读数重复推送
        status, first = post(f"{self.base}/temperatures",
                             {"logger_id": "L1", "at": "2026-09-05T10:10:00",
                              "temp_c": 3.0, "recorded_at": dep})
        status, second = post(f"{self.base}/temperatures",
                              {"logger_id": "L1", "at": "2026-09-05T10:10:00",
                               "temp_c": 3.0, "recorded_at": dep})
        self.assertTrue(first["duplicate"] is False)
        self.assertTrue(second["duplicate"] is True)

        # 承运方回调幂等
        status, c1 = post(f"{self.base}/carrier-callbacks",
                          {"shipment_id": "TR1", "status": "in_transit",
                           "occurred_at": dep, "idempotency_key": "cb-1", "at": dep})
        status, c2 = post(f"{self.base}/carrier-callbacks",
                          {"shipment_id": "TR1", "status": "in_transit",
                           "occurred_at": dep, "idempotency_key": "cb-1", "at": dep})
        self.assertFalse(c1["duplicate"])
        self.assertTrue(c2["duplicate"])

        # 补传全程读数、到达、分批签收
        readings = [{"logger_id": "L1",
                     "at": f"2026-09-05T{10 + h:02d}:{m:02d}:00", "temp_c": 3.0}
                    for h in range(0, 6) for m in (0, 15, 30, 45)]
        post(f"{self.base}/temperatures/batch", {"readings": readings})
        d1 = "2026-09-05T16:00:00"
        post(f"{self.base}/shipments/arrive", {"shipment_id": "TR1", "arrived_at": d1, "at": d1})
        status, _ = post(f"{self.base}/receipts",
                         {"order_id": "O1", "bouquet_ids": ["F1"], "received_at": d1,
                          "shipment_id": "TR1", "at": d1})
        self.assertEqual(status, 200)
        status, _ = post(f"{self.base}/receipts",
                         {"order_id": "O1", "bouquet_ids": ["F2"], "received_at": d1,
                          "shipment_id": "TR1", "at": d1})
        self.assertEqual(status, 200)
        status, bad = post(f"{self.base}/receipts",
                           {"order_id": "O1", "bouquet_ids": ["F1"], "received_at": d1,
                            "shipment_id": "TR1", "at": d1})
        self.assertEqual(status, 409)

        # 无开放异常 -> 结算成功，且只能结一次
        status, settlement = post(f"{self.base}/orders/O1/settle", {"at": d1})
        self.assertEqual(status, 200)
        self.assertEqual(settlement["rule_version"], "rule-v1-2026-08")
        status, again = post(f"{self.base}/orders/O1/settle", {"at": d1})
        self.assertEqual(status, 409)
        self.assertEqual(again["error"], "settlement_closed")

        # 三种角色视图与回放都可取
        status, cs = get(f"{self.base}/views/customer-service/orders/O1")
        self.assertEqual(status, 200)
        self.assertEqual(len(cs["bouquets"]), 2)
        status, grower = get(f"{self.base}/views/grower?greenhouse_id=G1")
        self.assertEqual(status, 200)
        self.assertEqual(len(grower["settled_lines"]), 2)
        status, mgmt = get(f"{self.base}/views/management")
        self.assertEqual(status, 200)
        self.assertTrue(any(o["order_id"] == "O1" for o in mgmt["orders"]))
        status, replay = get(f"{self.base}/orders/O1/replay")
        self.assertEqual(status, 200)
        self.assertGreater(len(replay["timeline"]), 10)

    def test_error_codes(self):
        status, body = get(f"{self.base}/orders/NOPE")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")
        status, body = post(f"{self.base}/bouquets/harvest",
                            {"shift_id": "X", "bouquet_ids": ["F9"]})
        self.assertEqual(status, 404)
        status, body = post(f"{self.base}/unknown", {})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
