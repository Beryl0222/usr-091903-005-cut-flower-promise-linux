"""鲜切花保鲜承诺领域端到端测试。

覆盖：
1. 花束来源在拼箱/拆箱/换车中持续保留；
2. 接单固化当时规则，后来放宽不溯及旧单；冷量不足拒绝承诺；
3. 断连补传、重复回调、分批签收均只生效一次；
4. 超温/断连/延误自动圈定受影响花束，补传后自动解除；
5. 一次结算：客赔、承运罚金、种植户扣款各归其责；
6. 事件流重建与订单全链路回放；
7. 三类角色视图。
"""

import os
import tempfile
import unittest

from freshness.app import FreshnessService
from freshness.clock import add_minutes, diff_minutes
from freshness.errors import Conflict, NotFound, RuleError, SettlementClosed
from freshness.projection import TEMP_READING
from freshness.rules import RuleBook
from freshness.store import EventStore
from freshness import views

T0 = "2026-09-05T08:00:00"   # v1 规则生效期内


def series(logger_id, start, end, step_min, temp_c):
    return [
        {"logger_id": logger_id, "at": add_minutes(start, m), "temp_c": temp_c}
        for m in range(0, diff_minutes(end, start) + 1, step_min)
    ]


def build_full_flow():
    """构造一单完整业务：两棚两品种、拼箱拆箱、换车、在途温度与签收。"""
    svc = FreshnessService()
    svc.register_seed_batch("SB-01", "卡罗拉", supplier="玉溪种苗A场", at=T0)
    svc.register_seed_batch("SB-02", "影星", supplier="玉溪种苗B场", at=T0)
    svc.register_greenhouse("GH-01", name="一号棚", at=T0)
    svc.register_greenhouse("GH-02", name="二号棚", at=T0)
    svc.perform_cut_shift("SH-01", "GH-01", "SB-01", shift_code="早班", at=T0)
    svc.perform_cut_shift("SH-02", "GH-02", "SB-02", shift_code="早班", at=T0)
    svc.harvest_bouquets("SH-01", ["B1", "B2", "B3"], maturity="commercial", at=T0)
    svc.harvest_bouquets("SH-02", ["X1", "X2"], maturity="tight_bud", at=T0)
    svc.record_postharvest(["B1", "B2", "B3"], precool_wait_min=20,
                           pulse_solution="STS+蔗糖", grading="A", at=add_minutes(T0, 30))
    svc.record_postharvest(["X1", "X2"], precool_wait_min=10,
                           pulse_solution="STS+蔗糖", grading="A", at=add_minutes(T0, 30))
    # 拼箱：5 束不同来源同箱
    svc.pack_box("BOX-1", ["B1", "B2", "B3", "X1", "X2"], logger_id="LG-BOX1",
                 at=add_minutes(T0, 60))
    # 拆箱：B3/X2 进入新箱（新记录器），来源继续保留
    svc.split_box("BOX-1", [{"box_id": "BOX-2", "bouquet_ids": ["B3", "X2"]}],
                  at=add_minutes(T0, 90))
    svc.bind_logger("LG-BOX2", "box", "BOX-2", at=add_minutes(T0, 90))
    return svc


class LineageTest(unittest.TestCase):
    def test_provenance_follows_pack_split_vehicle_change(self):
        svc = build_full_flow()
        dep = add_minutes(T0, 120)
        svc.create_shipment("TR-1", "云冷链", ["BOX-1", "BOX-2"], "V-滇A001",
                            vehicle_logger_id="LG-V1", route="玉溪-昆明", at=dep)
        svc.record_temperature_batch(series("LG-BOX1", dep, add_minutes(dep, 60), 10, 3.0), at=dep)
        svc.change_vehicle("TR-1", "V-滇B002", to_vehicle_logger_id="LG-V2",
                           at=add_minutes(dep, 60))
        b3 = svc.get_bouquet("B3")
        self.assertEqual(b3["seed_batch_id"], "SB-01")
        self.assertEqual(b3["greenhouse_id"], "GH-01")
        self.assertEqual(b3["shift_id"], "SH-01")
        kinds = [e["kind"] for e in b3["chain"]]
        self.assertEqual(
            kinds,
            ["harvested", "treated", "packed", "split", "loaded", "vehicle_changed"],
        )
        # 拆箱环节仍能指回原箱
        split_entry = next(e for e in b3["chain"] if e["kind"] == "split")
        self.assertEqual(split_entry["from_box_id"], "BOX-1")
        self.assertEqual(split_entry["box_id"], "BOX-2")
        # 采后记录未丢
        self.assertEqual(len(b3["treatment_ids"]), 1)
        self.assertEqual(b3["precool_wait_min"], 20)


class RuleFreezeTest(unittest.TestCase):
    def test_order_keeps_rule_version_at_acceptance(self):
        svc = build_full_flow()
        # 9 月 5 日接单 -> v1
        accepted_at = add_minutes(T0, 100)
        order = svc.accept_order("ORD-1", ["B1", "X1"], 200.0,
                                 channel="wholesale", customer="斗南花市",
                                 idempotency_key="ok-1", at=accepted_at)
        self.assertEqual(order["rule_version"], "rule-v1-2026-08")
        self.assertEqual(order["rule_snapshot"]["maturity_factors"]["open"], 0.75)
        # 9 月 11 日新采切的花接新单 -> v2（已放宽）
        later = "2026-09-11T08:00:00"
        svc.perform_cut_shift("SH-03", "GH-01", "SB-01", shift_code="早班", at=later)
        svc.harvest_bouquets("SH-03", ["B4"], maturity="open", at=later)
        order2 = svc.accept_order("ORD-2", ["B4"], 100.0,
                                  channel="wedding", customer="某婚礼", at=later)
        self.assertEqual(order2["rule_version"], "rule-v2-2026-09")
        self.assertEqual(order2["rule_snapshot"]["maturity_factors"]["open"], 0.85)
        # 旧单的快照没有被新版本影响
        again = svc.get_order("ORD-1")
        self.assertEqual(again["rule_snapshot"]["maturity_factors"]["open"], 0.75)
        self.assertEqual(
            again["arrival_window"]["max_hours"],
            order["arrival_window"]["max_hours"],
        )

    def test_low_remaining_cool_cannot_promise(self):
        svc = build_full_flow()
        dep = add_minutes(T0, 120)
        svc.create_shipment("TR-1", "云冷链", ["BOX-1"], "V-滇A001",
                            vehicle_logger_id="LG-V1", at=dep)
        # 30 小时高温，冷量被快速耗尽后才来接单
        svc.record_temperature_batch(
            series("LG-BOX1", dep, add_minutes(dep, 30 * 60), 10, 18.0), at=dep
        )
        with self.assertRaises(RuleError) as ctx:
            svc.accept_order("ORD-X", ["B1"], 100.0, at=add_minutes(dep, 30 * 60))
        reasons = ctx.exception.context["reasons"]
        self.assertTrue(any(r["reason"] == "remaining_below_floor" for r in reasons))

    def test_duplicate_accept_idempotency_key_rejected(self):
        svc = build_full_flow()
        at = add_minutes(T0, 100)
        svc.accept_order("ORD-1", ["B1"], 100.0, idempotency_key="K1", at=at)
        with self.assertRaises(Conflict):
            svc.accept_order("ORD-1", ["B1"], 100.0, idempotency_key="K1", at=at)


class IngestIdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_full_flow()
        self.dep = add_minutes(T0, 120)
        self.svc.create_shipment("TR-1", "云冷链", ["BOX-1", "BOX-2"], "V-滇A001",
                                 vehicle_logger_id="LG-V1", at=self.dep)
        self.svc.accept_order("ORD-1", ["B1", "B2", "B3", "X1", "X2"], 500.0,
                              channel="florist", customer="城市花店联盟", at=self.dep)

    def test_temperature_natural_key_dedup(self):
        r1 = self.svc.record_temperature("LG-BOX1", add_minutes(self.dep, 5), 3.0, at=self.dep)
        r2 = self.svc.record_temperature("LG-BOX1", add_minutes(self.dep, 5), 3.0, at=self.dep)
        self.assertFalse(r1["duplicate"])
        self.assertTrue(r2["duplicate"])
        readings = self.svc.state.logger_readings("LG-BOX1")
        self.assertEqual(len(readings), 1)

    def test_carrier_callback_idempotent(self):
        c1 = self.svc.carrier_callback("TR-1", "in_transit", self.dep, "CB-1", at=self.dep)
        c2 = self.svc.carrier_callback("TR-1", "in_transit", self.dep, "CB-1", at=self.dep)
        self.assertFalse(c1["duplicate"])
        self.assertTrue(c2["duplicate"])
        shipment = self.svc.state.shipments["TR-1"]
        self.assertEqual(len(shipment["callbacks"]), 1)

    def test_split_receipts_settle_each_bouquet_once(self):
        d1 = add_minutes(self.dep, 6 * 60)
        self.svc.shipment_arrived("TR-1", arrived_at=d1, at=d1)
        self.svc.confirm_receipt("ORD-1", ["B1", "B2"], d1, shipment_id="TR-1", at=d1)
        self.svc.confirm_receipt("ORD-1", ["B3", "X1", "X2"], d1, shipment_id="TR-1", at=d1)
        order = self.svc.get_order("ORD-1")
        self.assertEqual(order["status"], "received")
        self.assertEqual(len(order["receipts"]), 2)
        # 同一束花再签一次被拒绝
        with self.assertRaises(Conflict):
            self.svc.confirm_receipt("ORD-1", ["B1"], d1, shipment_id="TR-1", at=d1)


class ExceptionFlaggingTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_full_flow()
        self.dep = add_minutes(T0, 120)
        # 只有 BOX-1 上车；BOX-2 留仓，用来验证圈定范围
        self.svc.create_shipment("TR-1", "云冷链", ["BOX-1"], "V-滇A001",
                                 vehicle_logger_id="LG-V1", at=self.dep)
        self.svc.accept_order("ORD-1", ["B1", "B2"], 200.0, at=self.dep)
        self.svc.accept_order("ORD-2", ["B3", "X2"], 200.0, at=self.dep)

    def test_hot_reading_flags_only_bouquets_in_transit(self):
        at = add_minutes(self.dep, 30)
        # 超温读数来自箱内记录器（在途时箱记录器优先于车厢记录器）
        self.svc.record_temperature("LG-BOX1", at, 16.0, at=at)
        flags = self.svc.list_flags("ORD-1")
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["reason"], "temperature_excursion")
        self.assertEqual(sorted(flags[0]["bouquet_ids"]), ["B1", "B2"])
        # 留仓的 BOX-2 / 其他订单不被圈入
        self.assertEqual(self.svc.list_flags("ORD-2"), [])

    def test_vehicle_logger_hot_does_not_override_cool_box_logger(self):
        # 箱记录器贴近花：3℃；车厢记录器报 16℃ 不应圈定箱内花束
        at = add_minutes(self.dep, 30)
        self.svc.record_temperature("LG-BOX1", at, 3.0, at=at)
        self.svc.record_temperature("LG-V1", at, 16.0, at=at)
        self.assertEqual(self.svc.list_flags("ORD-1"), [])

    def test_repeated_hot_readings_merge_into_one_flag(self):
        for m in (20, 30, 40):
            at = add_minutes(self.dep, m)
            self.svc.record_temperature("LG-BOX1", at, 12.0, at=at)
        flags = self.svc.list_flags("ORD-1", status="open")
        self.assertEqual(len(flags), 1)
        self.assertEqual(sorted(flags[0]["bouquet_ids"]), ["B1", "B2"])
        self.assertLessEqual(flags[0]["window"]["from"], add_minutes(self.dep, 20))
        self.assertGreaterEqual(flags[0]["window"]["to"], add_minutes(self.dep, 40))

    def test_disconnect_flag_opened_then_cleared_by_backfill(self):
        # 全程理想读数，每 10 分钟一条 -> 无断连
        d1 = add_minutes(self.dep, 4 * 60)
        self.svc.record_temperature_batch(
            series("LG-BOX1", self.dep, d1, 10, 3.0), at=d1
        )
        self.svc.shipment_arrived("TR-1", arrived_at=d1, at=d1)
        # 制造 60 分钟空档后再传 -> 断连标志出现
        gap_at = add_minutes(d1, 120)
        self.svc.record_temperature("LG-BOX1", gap_at, 3.0, at=gap_at)
        flags = self.svc.list_flags("ORD-1")
        disconnect = [f for f in flags if f["reason"] == "logger_disconnect"]
        self.assertEqual(len(disconnect), 1)
        self.assertEqual(disconnect[0]["status"], "open")
        flag_id = disconnect[0]["id"]
        # 补传空档内的冷藏读数（断连补传）-> 自动重评并解除
        backfill = series("LG-BOX1", d1, gap_at, 10, 3.0)
        result = self.svc.record_temperature_batch(backfill, at=gap_at)
        self.assertGreaterEqual(result["accepted"], 1)
        flag = next(f for f in self.svc.list_flags("ORD-1") if f["id"] == flag_id)
        self.assertEqual(flag["status"], "cleared")
        self.assertTrue(any(h["action"] == "cleared" for h in flag["history"]))

    def test_late_delivery_blocks_settlement_until_ruled(self):
        d1 = add_minutes(self.dep, 4 * 60)
        self.svc.record_temperature_batch(series("LG-BOX1", self.dep, d1, 10, 3.0), at=d1)
        # 晚于承诺窗口（72 小时）到达
        late = add_minutes(self.dep, 80 * 60)
        # 承运方补传全程冷藏读数 -> 在途无断连，只剩延误事实
        self.svc.record_temperature_batch(series("LG-BOX1", d1, late, 10, 3.0), at=late)
        self.svc.shipment_arrived("TR-1", arrived_at=late, at=late)
        self.svc.confirm_receipt("ORD-1", ["B1", "B2"], late, shipment_id="TR-1", at=late)
        with self.assertRaises(Conflict) as ctx:
            self.svc.settle_order("ORD-1", at=late)
        self.assertTrue(
            any(f["reason"] == "late_delivery"
                for f in ctx.exception.context["open_flags"])
        )
        flag = next(f for f in self.svc.list_flags("ORD-1") if f["reason"] == "late_delivery")
        # 承运方补证（堵车证明），系统不自动免责，管理层终裁
        self.svc.submit_evidence(flag["id"], "carrier", kind="proof",
                                 note="高速管制堵车 3 小时，附通行记录", at=late)
        self.svc.rule_flag(flag["id"], "carrier", "carrier_liable",
                           reason="路况延误由承运方承担", at=late)
        settlement = self.svc.settle_order("ORD-1", at=late)
        self.assertGreater(settlement["customer_payout_total"], 0)
        self.assertEqual(settlement["grower_deduction_total"], 0.0)
        self.assertEqual(settlement["carrier_penalty_total"],
                         settlement["customer_payout_total"])
        # 二次结算被拒绝
        with self.assertRaises(SettlementClosed):
            self.svc.settle_order("ORD-1", at=late)
        # 既有结果仍可查
        self.assertEqual(self.svc.get_settlement("ORD-1")["order_id"], "ORD-1")


class GrowerFaultSettlementTest(unittest.TestCase):
    def test_arrival_quality_ruled_grower_becomes_deduction(self):
        svc = build_full_flow()
        dep = add_minutes(T0, 120)
        svc.create_shipment("TR-1", "云冷链", ["BOX-1", "BOX-2"], "V-滇A001",
                            vehicle_logger_id="LG-V1", at=dep)
        svc.accept_order("ORD-1", ["B1", "B2", "B3", "X1", "X2"], 500.0, at=dep)
        d1 = add_minutes(dep, 6 * 60)
        svc.record_temperature_batch(series("LG-BOX1", dep, d1, 10, 3.0), at=d1)
        svc.record_temperature_batch(series("LG-BOX2", dep, d1, 10, 3.0), at=d1)
        svc.shipment_arrived("TR-1", arrived_at=d1, at=d1)
        # B3 收货时机械损伤
        svc.confirm_receipt("ORD-1", ["B1", "B2", "X1", "X2"], d1, shipment_id="TR-1", at=d1)
        svc.confirm_receipt("ORD-1", ["B3"], d1, condition="damaged",
                            shipment_id="TR-1", note="花瓣压伤", at=d1)
        flag = next(f for f in svc.list_flags("ORD-1") if f["reason"] == "arrival_quality")
        svc.submit_evidence(flag["id"], "grower", note="分级时已发现包扎偏松", at=d1)
        svc.rule_flag(flag["id"], "grower", "grower_liable",
                      reason="包装防护不足属产地问题", at=d1)
        settlement = svc.settle_order("ORD-1", at=d1)
        line_b3 = next(l for l in settlement["lines"] if l["bouquet_id"] == "B3")
        self.assertEqual(line_b3["compensation_ratio"], 0.50)
        self.assertEqual(line_b3["customer_payout"], line_b3["grower_deduction"])
        self.assertEqual(line_b3["carrier_penalty"], 0.0)
        self.assertEqual(settlement["grower_deduction_total"], line_b3["customer_payout"])
        # 种植户视图能逐笔核对
        grower = views.grower_view(svc, greenhouse_id="GH-01")
        self.assertEqual(grower["deduction_total"], line_b3["customer_payout"])
        row = next(r for r in grower["deduction_lines"] if r["bouquet_id"] == "B3")
        self.assertEqual(row["seed_batch_id"], "SB-01")
        self.assertEqual(row["carrier_faults"], [])


class ReplayAndRebuildTest(unittest.TestCase):
    def test_full_replay_and_rebuild_from_event_log(self):
        svc = build_full_flow()
        dep = add_minutes(T0, 120)
        svc.create_shipment("TR-1", "云冷链", ["BOX-1", "BOX-2"], "V-滇A001",
                            vehicle_logger_id="LG-V1", at=dep)
        svc.accept_order("ORD-1", ["B1"], 100.0, at=dep)
        d1 = add_minutes(dep, 6 * 60)
        svc.record_temperature_batch(series("LG-BOX1", dep, d1, 10, 3.0), at=d1)
        svc.shipment_arrived("TR-1", arrived_at=d1, at=d1)
        svc.confirm_receipt("ORD-1", ["B1"], d1, shipment_id="TR-1", at=d1)
        svc.settle_order("ORD-1", at=d1)

        replay = svc.replay("ORD-1")
        types = [e["type"] for e in replay["timeline"]]
        for expected in [
            "SeedBatchRegistered", "GreenhouseRegistered", "CutShiftPerformed",
            "BouquetHarvested", "PostHarvestTreatmentRecorded",
            "BoxPacked", "BoxSplit", "LoggerBound",
            "ShipmentCreated", "TemperatureReadingRecorded",
            "OrderAccepted", "ShipmentArrived",
            "DeliveryReceiptConfirmed", "SettlementCreated",
        ]:
            self.assertIn(expected, types, f"回放缺少 {expected}")
        self.assertEqual(replay["rule_version_at_acceptance"], "rule-v1-2026-08")
        seqs = [e["seq"] for e in replay["timeline"]]
        self.assertEqual(seqs, sorted(seqs))
        self.assertIsNotNone(replay["settlement"])

        # 从事件文件重建，状态一致
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.jsonl")
            rebuilt = FreshnessService(EventStore(path))
            for event in svc.store.all():
                rebuilt.store.append(event["type"], event["payload"], event["at"],
                                     actor=event.get("actor"))
            rebuilt = FreshnessService(EventStore(path))
            self.assertEqual(rebuilt.get_order("ORD-1")["rule_version"], "rule-v1-2026-08")
            self.assertEqual(rebuilt.get_settlement("ORD-1")["customer_payout_total"],
                             svc.get_settlement("ORD-1")["customer_payout_total"])
            self.assertEqual(len(rebuilt.replay("ORD-1")["timeline"]), len(types))

    def test_custom_rule_survives_event_log_rebuild(self):
        svc = build_full_flow()
        relaxed = dict(svc.rulebook.get("rule-v1-2026-08"))
        relaxed.pop("version")
        svc.publish_rule("rule-custom-x", "2026-09-20T00:00:00", relaxed,
                         at="2026-09-19T10:00:00")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.jsonl")
            store = EventStore(path)
            for event in svc.store.all():
                store.append(event["type"], event["payload"], event["at"],
                             actor=event.get("actor"))
            rebuilt = FreshnessService(EventStore(path))
            rule = rebuilt.rulebook.effective_at("2026-09-20T08:00:00")
            self.assertEqual(rule["version"], "rule-custom-x")

    def test_management_and_cs_views(self):
        svc = build_full_flow()
        dep = add_minutes(T0, 120)
        svc.create_shipment("TR-1", "云冷链", ["BOX-1"], "V-滇A001",
                            vehicle_logger_id="LG-V1", at=dep)
        svc.accept_order("ORD-1", ["B1", "B2"], 200.0, customer="斗南花市", at=dep)
        cs = views.customer_service_view(svc, "ORD-1")
        self.assertEqual(cs["rule_version_frozen_at_acceptance"], "rule-v1-2026-08")
        self.assertIn("不影响本单", cs["rule_freeze_explanation"])
        self.assertEqual(len(cs["bouquets"]), 2)
        mgmt = views.management_view(svc)
        self.assertEqual(len(mgmt["orders"]), 1)
        self.assertGreaterEqual(len(mgmt["rule_versions"]), 2)


if __name__ == "__main__":
    unittest.main()
