"""履约测试：幂等采集、超温/延误圈定、补证门控、一次结算与种植扣款。"""

import unittest

from freshkeep.fulfillment import FulfillmentError, LIABILITY_CARRIER, ADJUDICATED

from test_support import build_app, pack_and_load


def cold_readings(app, logger="LOG-A", times=("02:00", "10:00", "18:00"),
                  day="2026-05-01", temp=3.0, seq0=1):
    seq = seq0
    for hm in times:
        app.monitoring.record_reading(
            logger, seq, f"{day}T{hm}:00Z", temp)
        seq += 1


class FulfillmentTest(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        pack_and_load(self.app)
        self.clk = self.app.clock
        self.app.ordering.place_order(
            "ORD-F1",
            [{"line_id": "L1", "bouquet_ids": ["BQ-1", "BQ-2", "BQ-3"],
              "channel": "wedding", "unit_price": 100,
              "planned_transit_hours": 40}],
            customer="海之约婚庆", placed_at="2026-05-01T00:00:00Z")

    def _receive_all_on_time(self):
        self.clk.set("2026-05-03T00:00:00Z")
        self.app.fulfillment.sign_receipt(
            "RC-1", "ORD-F1", "L1", ["BQ-1"], "2026-05-02T16:00:00Z",
            receiver="婚礼管家")
        self.app.fulfillment.sign_receipt(
            "RC-2", "ORD-F1", "L1", ["BQ-2"], "2026-05-02T17:00:00Z",
            receiver="婚礼管家")
        return self.app.fulfillment.sign_receipt(
            "RC-3", "ORD-F1", "L1", ["BQ-3"], "2026-05-02T18:00:00Z",
            receiver="婚礼管家")

    # ---- 幂等 -----------------------------------------------------------

    def test_carrier_callback_and_reading_duplicates_are_idempotent(self):
        r1 = self.app.fulfillment.carrier_event(
            "CE-1", "ORD-F1", "departed", "2026-05-01T01:00:00Z",
            vehicle_id="CAR-1")
        r2 = self.app.fulfillment.carrier_event(
            "CE-1", "ORD-F1", "departed", "2026-05-01T01:00:00Z",
            vehicle_id="CAR-1")
        self.assertFalse(r1["deduplicated"])
        self.assertTrue(r2["deduplicated"])
        self.assertEqual(len(self.app.store.delivery_events), 1)
        a = self.app.monitoring.record_reading("LOG-A", 90, "2026-05-01T12:00:00Z", 3.0)
        b = self.app.monitoring.record_reading("LOG-A", 90, "2026-05-01T12:00:00Z", 3.0)
        self.assertFalse(a["deduplicated"])
        self.assertTrue(b["deduplicated"])

    def test_split_receipts_settle_once_and_double_sign_rejected(self):
        cold_readings(self.app)
        result = self._receive_all_on_time()
        self.assertTrue(result["fully_received"])
        # 重复回执幂等
        dup = self.app.fulfillment.sign_receipt(
            "RC-1", "ORD-F1", "L1", ["BQ-1"], "2026-05-02T16:00:00Z")
        self.assertTrue(dup["deduplicated"])
        # 换个 receipt_id 也不能再签收同一束
        with self.assertRaises(FulfillmentError):
            self.app.fulfillment.sign_receipt(
                "RC-X", "ORD-F1", "L1", ["BQ-1"], "2026-05-02T16:30:00Z")
        settlement = self.app.fulfillment.settle_line("ORD-F1", "L1")
        self.assertFalse(settlement["deduplicated"])
        again = self.app.fulfillment.settle_line("ORD-F1", "L1")
        self.assertTrue(again["deduplicated"])
        self.assertEqual(len(self.app.store.settlements), 1)

    # ---- 超温圈定与补证门控 ----------------------------------------------

    def test_excursion_circles_bouquets_and_blocks_settlement_until_adjudicated(self):
        # 03:00-04:00 连续 14℃（换车前 CAR-1 上），构成 major 超温
        self.app.monitoring.record_reading("LOG-A", 1, "2026-05-01T02:00:00Z", 3.0)
        self.app.monitoring.record_reading("LOG-A", 2, "2026-05-01T03:00:00Z", 14.0)
        self.app.monitoring.record_reading("LOG-A", 3, "2026-05-01T04:00:00Z", 14.0)
        self.app.monitoring.record_reading("LOG-A", 4, "2026-05-01T05:00:00Z", 3.0)
        self._receive_all_on_time()
        incidents = self.app.fulfillment.list_incidents("ORD-F1")
        temp_inc = [i for i in incidents if i["type"] == "temperature_excursion"]
        self.assertEqual(len(temp_inc), 1)
        inc = temp_inc[0]
        self.assertEqual(inc["severity"], "major")
        self.assertEqual(inc["facts"]["vehicle_id"], "CAR-1")
        self.assertEqual(inc["affected_bouquets"]["L1"], ["BQ-1", "BQ-2", "BQ-3"])
        # 等待补证期间不能结算
        with self.assertRaises(FulfillmentError):
            self.app.fulfillment.settle_line("ORD-F1", "L1")
        # 补证 + 裁定承运方责任后可以结算
        self.app.fulfillment.submit_evidence(
            inc["incident_id"], "carrier", "冷机故障一小时", evidence_ref="EVF-9")
        self.app.fulfillment.adjudicate(
            inc["incident_id"], LIABILITY_CARRIER, note="承运方冷机故障")
        settlement = self.app.fulfillment.settle_line("ORD-F1", "L1")["settlement"]
        # major 赔付 50%：3 束 × 100 × 50% = 150
        self.assertEqual(settlement["refund_by_party"]["carrier"], 150.0)
        self.assertEqual(settlement["customer_refund"], 150.0)
        self.assertEqual(settlement["rule_version_id"], "R-2026-01")
        # 赔付依据可向客户说明
        rationale = [r for r in settlement["rationale"]
                     if r["type"] == "temperature_excursion"][0]
        self.assertEqual(rationale["basis"]["rule_thresholds"]["peak_temp_c"], 14.0)

    def test_only_bouquets_actually_in_hot_container_are_circled(self):
        # BQ-4/BQ-5 走独立箱 BOX-B，即使同订单也不应被 BOX-A 的超温圈定
        self.app.lineage.pack("EV-PACK-B", "BOX-B", ["BQ-4", "BQ-5"],
                              "2026-04-30T23:00:00Z")
        self.app.lineage.bind_logger("LOG-B", "BOX-B", "2026-04-30T23:00:00Z")
        self.app.ordering.place_order(
            "ORD-F2",
            [{"line_id": "L1", "bouquet_ids": ["BQ-4", "BQ-5"],
              "channel": "florist", "unit_price": 100}],
            placed_at="2026-05-01T00:30:00Z")
        self.app.monitoring.record_reading("LOG-A", 1, "2026-05-01T03:00:00Z", 13.0)
        self.app.monitoring.record_reading("LOG-A", 2, "2026-05-01T04:00:00Z", 13.0)
        self.app.monitoring.record_reading("LOG-B", 1, "2026-05-01T03:00:00Z", 3.0)
        self.app.monitoring.record_reading("LOG-B", 2, "2026-05-01T04:00:00Z", 3.0)
        self.clk.set("2026-05-03T00:00:00Z")
        self.app.fulfillment.sign_receipt(
            "RC2-1", "ORD-F2", "L1", ["BQ-4"], "2026-05-02T16:00:00Z")
        self.app.fulfillment.sign_receipt(
            "RC2-2", "ORD-F2", "L1", ["BQ-5"], "2026-05-02T16:00:00Z")
        inc = [i for i in self.app.fulfillment.list_incidents("ORD-F2")
               if i["type"] == "temperature_excursion"]
        self.assertEqual(inc, [])

    def test_brief_spike_below_duration_threshold_is_not_incident(self):
        self.app.monitoring.record_reading("LOG-A", 1, "2026-05-01T02:00:00Z", 3.0)
        self.app.monitoring.record_reading("LOG-A", 2, "2026-05-01T03:00:00Z", 13.0)
        self.app.monitoring.record_reading("LOG-A", 3, "2026-05-01T03:10:00Z", 3.0)
        cold_readings(self.app, times=("06:00", "12:00"), seq0=10)
        self._receive_all_on_time()
        self.assertEqual(
            [i for i in self.app.fulfillment.list_incidents("ORD-F1")
             if i["type"] == "temperature_excursion"], [])

    def test_rescan_is_idempotent(self):
        self.app.monitoring.record_reading("LOG-A", 1, "2026-05-01T03:00:00Z", 14.0)
        self.app.monitoring.record_reading("LOG-A", 2, "2026-05-01T04:00:00Z", 14.0)
        self._receive_all_on_time()
        self.app.fulfillment.scan_order("ORD-F1")
        self.app.fulfillment.scan_all_orders()
        temp_inc = [i for i in self.app.fulfillment.list_incidents("ORD-F1")
                    if i["type"] == "temperature_excursion"]
        self.assertEqual(len(temp_inc), 1)

    def test_late_backfill_after_receipt_can_still_circle_incident(self):
        # 先全部签收（当时只有正常读数），再补传一批迟到数据，其中暴露超温
        self.app.monitoring.record_reading("LOG-A", 1, "2026-05-01T02:00:00Z", 3.0)
        self.clk.set("2026-05-03T00:00:00Z")
        self.app.fulfillment.sign_receipt(
            "RC-1", "ORD-F1", "L1", ["BQ-1", "BQ-2", "BQ-3"],
            "2026-05-02T18:00:00Z")
        self.assertEqual(
            [i for i in self.app.fulfillment.list_incidents("ORD-F1")
             if i["type"] == "temperature_excursion"], [])
        # 迟到的补传数据显示 03:00-04:00 曾超温
        self.app.monitoring.backfill_readings(
            "LOG-A",
            [{"seq": 20, "read_at": "2026-05-01T03:00:00Z", "temp_c": 13.0},
             {"seq": 21, "read_at": "2026-05-01T04:00:00Z", "temp_c": 13.0}],
            event_id="BF-LATE")
        self.app.fulfillment.scan_order("ORD-F1")
        inc = [i for i in self.app.fulfillment.list_incidents("ORD-F1")
               if i["type"] == "temperature_excursion"]
        self.assertEqual(len(inc), 1)
        self.assertEqual(inc[0]["affected_bouquets"]["L1"],
                         ["BQ-1", "BQ-2", "BQ-3"])
        # 即使已签收，未裁定事故依然阻止结算
        with self.assertRaises(FulfillmentError):
            self.app.fulfillment.settle_line("ORD-F1", "L1")

    # ---- 断连补传 --------------------------------------------------------

    def test_gap_backfilled_closes_fact_and_does_not_block_settlement(self):
        self.app.monitoring.record_reading("LOG-A", 1, "2026-05-01T02:00:00Z", 3.0)
        self.app.monitoring.report_gap(
            logger_id="LOG-A", gap_id="GAP-1",
            disconnected_at="2026-05-01T02:00:00Z",
            resumed_at="2026-05-01T06:00:00Z", reason="设备断电")
        bf = self.app.monitoring.backfill_readings(
            "LOG-A",
            [{"seq": 3, "read_at": "2026-05-01T03:00:00Z", "temp_c": 3.0},
             {"seq": 4, "read_at": "2026-05-01T04:00:00Z", "temp_c": 3.0},
             {"seq": 5, "read_at": "2026-05-01T05:00:00Z", "temp_c": 3.0}],
            gap_id="GAP-1", event_id="BF-1")
        self.assertEqual(bf["inserted"], 3)
        # 补传批次重发幂等
        bf_dup = self.app.monitoring.backfill_readings(
            "LOG-A",
            [{"seq": 3, "read_at": "2026-05-01T03:00:00Z", "temp_c": 3.0}],
            gap_id="GAP-1", event_id="BF-1")
        self.assertTrue(bf_dup["deduplicated"])
        self._receive_all_on_time()
        gap_inc = [i for i in self.app.fulfillment.list_incidents("ORD-F1")
                   if i["type"] == "temperature_gap"]
        self.assertEqual(len(gap_inc), 1)
        self.assertEqual(gap_inc[0]["status"], ADJUDICATED)
        self.assertEqual(gap_inc[0]["liability"], "none")
        # 补传数据进入完整时间线
        timeline = self.app.monitoring.timeline("LOG-A")
        self.assertEqual(len(timeline), 4)
        self.assertTrue(any(r["backfilled"] for r in timeline))
        settlement = self.app.fulfillment.settle_line("ORD-F1", "L1")["settlement"]
        self.assertEqual(settlement["customer_refund"], 0.0)

    def test_unexplained_gap_blocks_settlement_then_evidence_allows_it(self):
        self.app.monitoring.record_reading("LOG-A", 1, "2026-05-01T02:00:00Z", 3.0)
        # 02:00-10:00 长时间断连且无补传
        self.app.monitoring.report_gap(
            logger_id="LOG-A", gap_id="GAP-2",
            disconnected_at="2026-05-01T02:00:00Z",
            resumed_at="2026-05-01T10:00:00Z", reason="记录仪损坏")
        cold_readings(self.app, times=("10:00", "18:00"), seq0=20)
        self._receive_all_on_time()
        gap_inc = [i for i in self.app.fulfillment.list_incidents("ORD-F1")
                   if i["type"] == "temperature_gap"][0]
        self.assertEqual(gap_inc["status"], "awaiting_evidence")
        with self.assertRaises(FulfillmentError):
            self.app.fulfillment.settle_line("ORD-F1", "L1")
        self.app.fulfillment.submit_evidence(
            gap_inc["incident_id"], "carrier", "记录仪损坏，冷链本身正常",
            evidence_ref="REPAIR-1")
        self.app.fulfillment.adjudicate(
            gap_inc["incident_id"], LIABILITY_CARRIER,
            note="无温度数据，按不利于承运方的保守温度计算")
        settlement = self.app.fulfillment.settle_line("ORD-F1", "L1")["settlement"]
        self.assertGreater(settlement["customer_refund"], 0.0)
        self.assertEqual(
            settlement["refund_by_party"]["carrier"], settlement["customer_refund"])

    # ---- 延误 ------------------------------------------------------------

    def test_delivery_delay_is_detected_and_compensated(self):
        for day, seqs in (("2026-05-01", range(1, 4)),
                          ("2026-05-02", range(4, 7)),
                          ("2026-05-03", range(7, 10))):
            for seq, hm in zip(seqs, ("02:00", "10:00", "18:00")):
                self.app.monitoring.record_reading(
                    "LOG-A", seq, f"{day}T{hm}:00Z", 3.0)
        self.clk.set("2026-05-05T00:00:00Z")
        # 承诺最晚 5/4 01:00（含 60 分钟容忍），5/4 08:00 才全部签收
        self.app.fulfillment.sign_receipt(
            "RC-1", "ORD-F1", "L1", ["BQ-1", "BQ-2", "BQ-3"],
            "2026-05-04T08:00:00Z")
        delay = [i for i in self.app.fulfillment.list_incidents("ORD-F1")
                 if i["type"] == "delivery_delay"][0]
        self.assertEqual(delay["facts"]["late_minutes"], 420.0)
        self.assertEqual(delay["severity"], "major")
        self.app.fulfillment.adjudicate(delay["incident_id"], LIABILITY_CARRIER,
                                        note="干线车辆调度延误")
        settlement = self.app.fulfillment.settle_line("ORD-F1", "L1")["settlement"]
        # 延误 ≥360 分钟档：40%
        self.assertEqual(settlement["refund_by_party"]["carrier"], 120.0)

    # ---- 种植端扣款 ------------------------------------------------------

    def test_grower_precool_wait_deduction_is_itemized(self):
        self.app.lineage.pack("EV-PACK-9", "BOX-B", ["BQ-9"],
                              "2026-05-01T03:00:00Z")
        self.app.ordering.place_order(
            "ORD-F9",
            [{"line_id": "L1", "bouquet_ids": ["BQ-9"],
              "channel": "florist", "unit_price": 90}],
            placed_at="2026-05-01T03:00:00Z")
        self.clk.set("2026-05-03T00:00:00Z")
        self.app.fulfillment.sign_receipt(
            "RC9", "ORD-F9", "L1", ["BQ-9"], "2026-05-02T12:00:00Z")
        settlement = self.app.fulfillment.settle_line("ORD-F9", "L1")["settlement"]
        # 等待 3h - 宽限 1h = 2h × 2%/h = 4% → 3.6 元
        self.assertEqual(settlement["grower_precool_deduction"], 3.6)
        self.assertEqual(settlement["grower_payout"], 86.4)
        detail = settlement["per_bouquet"][0]
        self.assertEqual(detail["precool_wait_hours"], 3.0)
        self.assertEqual(detail["precool_deduction_ratio"], 0.04)


if __name__ == "__main__":
    unittest.main()
