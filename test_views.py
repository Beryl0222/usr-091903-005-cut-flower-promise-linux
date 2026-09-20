"""三方视图测试：客服可说明、种植户可核对、管理层可回放。"""

import unittest

from test_support import build_app, pack_and_load


class ViewsTest(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        pack_and_load(self.app)
        for seq, hm in enumerate(("02:00", "10:00", "18:00"), start=1):
            self.app.monitoring.record_reading(
                "LOG-A", seq, f"2026-05-01T{hm}:00Z", 3.0)
        self.app.ordering.place_order(
            "ORD-V1",
            [{"line_id": "L1", "bouquet_ids": ["BQ-1", "BQ-2"],
              "channel": "wedding", "unit_price": 100,
              "planned_transit_hours": 40}],
            customer="海之约婚庆", placed_at="2026-05-01T00:00:00Z")
        # 一笔超温事故走完补证、裁定
        self.app.monitoring.record_reading(
            "LOG-A", 9, "2026-05-01T03:30:00Z", 13.0)
        self.app.monitoring.record_reading(
            "LOG-A", 10, "2026-05-01T04:30:00Z", 13.0)
        self.app.clock.set("2026-05-03T00:00:00Z")
        self.app.fulfillment.sign_receipt(
            "RC-1", "ORD-V1", "L1", ["BQ-1"], "2026-05-02T16:00:00Z")
        self.app.fulfillment.sign_receipt(
            "RC-2", "ORD-V1", "L1", ["BQ-2"], "2026-05-02T17:00:00Z")
        inc = [i for i in self.app.fulfillment.list_incidents("ORD-V1")
               if i["type"] == "temperature_excursion"][0]
        self.incident_id = inc["incident_id"]
        self.app.fulfillment.submit_evidence(
            self.incident_id, "carrier", "冷机故障", evidence_ref="EVF-1")
        self.app.fulfillment.adjudicate(
            self.incident_id, "carrier", note="承运方责任")
        self.settlement = self.app.fulfillment.settle_line("ORD-V1", "L1")["settlement"]

    def test_customer_service_view_explains_promise_and_compensation(self):
        view = self.app.views.customer_service_view("ORD-V1")
        self.assertEqual(view["rule_version_id"], "R-2026-01")
        line = view["lines"][0]
        self.assertIsNotNone(line["promise"]["promised_vase_days"])
        self.assertEqual(line["promise_basis"]["rule_version_id"], "R-2026-01")
        # 赔付依据可说明：责任方、比例、阈值事实
        comp = line["compensation"]
        self.assertEqual(comp["customer_refund_total"], 100.0)
        basis = comp["rationale"][0]
        self.assertEqual(basis["liability"], "carrier")
        self.assertEqual(basis["basis"]["rule_thresholds"]["peak_temp_c"], 13.0)
        # 两批签收都可向客户出示
        self.assertEqual(len(line["receipts"]), 2)

    def test_waiting_evidence_view_tells_agent_what_is_pending(self):
        self.app.lineage.pack("EV-PACK-C", "BOX-C", ["BQ-3"],
                              "2026-05-01T00:30:00Z")
        self.app.lineage.bind_logger("LOG-C", "BOX-C", "2026-05-01T00:30:00Z")
        self.app.ordering.place_order(
            "ORD-V2",
            [{"line_id": "L1", "bouquet_ids": ["BQ-3"],
              "channel": "florist", "unit_price": 100}],
            placed_at="2026-05-01T00:30:00Z")
        self.app.monitoring.report_gap(
            logger_id="LOG-C", gap_id="GAP-C",
            disconnected_at="2026-05-01T03:00:00Z",
            resumed_at="2026-05-01T09:00:00Z")
        self.app.clock.set("2026-05-03T00:00:00Z")
        self.app.fulfillment.sign_receipt(
            "RC-C", "ORD-V2", "L1", ["BQ-3"], "2026-05-02T16:00:00Z")
        view = self.app.views.customer_service_view("ORD-V2")
        claim = view["lines"][0]["claim"]
        self.assertEqual(claim["status"], "waiting_evidence")
        self.assertTrue(claim["pending_incident_ids"])
        self.assertIn("等待责任方补证", claim["message"])

    def test_grower_view_itemizes_deductions_with_rule_basis(self):
        # 再来一笔含预冷等待扣款的订单（BQ-9 属于 G-200）
        self.app.lineage.pack("EV-PACK-9", "BOX-B", ["BQ-9"],
                              "2026-05-01T03:00:00Z")
        self.app.lineage.bind_logger("LOG-9", "BOX-B", "2026-05-01T03:00:00Z")
        self.app.ordering.place_order(
            "ORD-V9",
            [{"line_id": "L1", "bouquet_ids": ["BQ-9"],
              "channel": "florist", "unit_price": 90}],
            placed_at="2026-05-01T03:00:00Z")
        self.app.fulfillment.sign_receipt(
            "RC-9", "ORD-V9", "L1", ["BQ-9"], "2026-05-02T12:00:00Z")
        self.app.fulfillment.settle_line("ORD-V9", "L1")
        view = self.app.views.grower_view("G-200")
        self.assertEqual(view["settlement_count"], 1)
        row = view["rows"][0]
        self.assertEqual(row["deductions"]["precool_wait"]["amount"], 3.6)
        rule = row["deductions"]["precool_wait"]["rule"]
        self.assertEqual(rule["warning_hours"], 1.0)
        self.assertEqual(rule["rule_version_id"], "R-2026-01")
        self.assertEqual(row["net_payout"], 86.4)
        # 无关种植户看不到这笔
        self.assertEqual(self.app.views.grower_view("G-NOBODY")["settlement_count"], 0)
        # G-200 只看到自己的 ORD-V9，看不到 G-100 的 ORD-V1
        g200 = self.app.views.grower_view("G-200")
        self.assertEqual({r["order_id"] for r in g200["rows"]}, {"ORD-V9"})

    def test_management_replay_covers_harvest_to_claim_in_order(self):
        replay = self.app.views.replay_order("ORD-V1")
        kinds = [t["kind"] for t in replay["timeline"]]
        self.assertEqual(kinds[0], "harvest")
        self.assertIn("settled", kinds)
        for earlier, later in zip(kinds, kinds[1:]):
            self.assertIsNotNone(earlier)
        # 关键决定都在场
        self.assertIn("order_placed", kinds)
        self.assertIn("incident_detected", kinds)
        self.assertIn("evidence_submitted", kinds)
        self.assertIn("incident_adjudicated", kinds)
        self.assertIn("claim_opened", kinds)
        # 时间线单调不下降
        ats = [t["at"] for t in replay["timeline"]]
        self.assertEqual(ats, sorted(ats))
        # 结算快照与订单冻结规则一致
        self.assertEqual(replay["settlements"][0]["rule_version_id"], "R-2026-01")

    def test_replay_lists_newer_versions_but_declares_no_retroactivity(self):
        self.app.rules.add_version(
            "R-2026-09", "2026-09-01T00:00:00Z",
            params_overrides={"compensation": {"major_excursion_refund_ratio": 0.05}},
            note="秋季放宽")
        replay = self.app.views.replay_order("ORD-V1")
        newer = [v["version_id"] for v in replay["rule_versions_published_after_order"]]
        self.assertIn("R-2026-09", newer)
        self.assertEqual(replay["frozen_rule_version_id"], "R-2026-01")
        # 已结算金额不随新规则改变
        self.assertEqual(replay["settlements"][0]["customer_refund"], 100.0)


if __name__ == "__main__":
    unittest.main()
