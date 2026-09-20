"""接单承诺测试：规则快照、剩余冷量窗口、不能用新规则解释旧订单。"""

import unittest

from freshkeep import coldlife

from test_support import build_app, pack_and_load


class PromiseTest(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        pack_and_load(self.app)

    def test_promise_uses_cold_budget_and_freezes_rule(self):
        order = self.app.ordering.place_order(
            "ORD-P1",
            [{"line_id": "L1", "bouquet_ids": ["BQ-1", "BQ-2"],
              "channel": "wedding", "unit_price": 120,
              "planned_transit_hours": 40}],
            customer="海之约婚庆", placed_at="2026-05-01T00:00:00Z")
        line = order["lines"][0]
        self.assertEqual(line["promise"]["status"], "promised")
        self.assertIn("promised_vase_days", line["promise"])
        self.assertEqual(order["rule_version_id"], "R-2026-01")
        # 到货窗口不晚于接单 + 渠道最大在途时限
        self.assertLessEqual(
            line["promise"]["arrival_window"]["latest_arrival_at"],
            "2026-05-04T00:00:00.000Z")
        # 每束花都有冻结来源
        self.assertEqual(
            set(line["frozen_provenance"]), {"BQ-1", "BQ-2"})
        self.assertEqual(
            line["frozen_provenance"]["BQ-1"]["seed_batch_id"], "SB-ROSE")

    def test_window_contracts_with_harvest_ripeness_and_precool_wait(self):
        # BQ-9 成熟度 4、预冷等待 3 小时：剩余冷量更少，窗口应更短
        self.app.lineage.pack("EV-PACK-B", "BOX-B", ["BQ-9"],
                              "2026-05-01T03:00:00Z")
        order = self.app.ordering.place_order(
            "ORD-P2",
            [{"line_id": "L1", "bouquet_ids": ["BQ-9"],
              "channel": "wedding", "unit_price": 90}],
            placed_at="2026-05-01T03:00:00Z")
        line = order["lines"][0]
        self.assertEqual(line["promise"]["status"], "promised")
        # 成熟度 4 基础瓶插只有 5.5 天，还要满足婚礼 ≥5 天，窗口显著短于 72 小时
        self.assertLess(line["promise"]["planned_transit_hours"], 72)

    def test_plan_beyond_window_is_rejected_with_reason(self):
        # 超出可承诺窗口的行不产生承诺，而不是悄悄按固定天数答应
        order = self.app.ordering.place_order(
            "ORD-P3",
            [{"line_id": "L1", "bouquet_ids": ["BQ-1"],
              "channel": "wedding", "planned_transit_hours": 200}],
            placed_at="2026-05-01T00:00:00Z")
        promise = order["lines"][0]["promise"]
        self.assertEqual(promise["status"], "rejected")
        self.assertTrue(promise["reason"])
        self.assertNotIn("promised_vase_days", promise)

    def test_worst_bouquet_governs_multi_bouquet_line(self):
        # BQ-2 状态好，BQ-9 状态差；一行内按最差束承诺
        order = self.app.ordering.place_order(
            "ORD-P4",
            [{"line_id": "L1", "bouquet_ids": ["BQ-2", "BQ-9"],
              "channel": "florist", "unit_price": 80}],
            placed_at="2026-05-01T03:00:00Z")
        line = order["lines"][0]
        rem = {b["bouquet_id"]: b["remaining_budget_hours"]
               for b in line["promise"]["per_bouquet"].values()}
        self.assertLess(rem["BQ-9"], rem["BQ-2"])

    def test_later_relaxed_rules_do_not_reinterpret_old_order(self):
        order = self.app.ordering.place_order(
            "ORD-P5",
            [{"line_id": "L1", "bouquet_ids": ["BQ-1"],
              "channel": "wedding", "unit_price": 100,
              "planned_transit_hours": 40}],
            placed_at="2026-05-01T00:00:00Z")
        original_window = order["lines"][0]["promise"]["arrival_window"]
        original_params = order["lines"][0]["resolved_params"]["BQ-1"]
        # 九月大幅放宽：最低瓶插 2 天、最大在途 120 小时、赔付减半
        self.app.rules.add_version(
            "R-2026-09", "2026-09-01T00:00:00Z",
            params_overrides={
                "min_vase_days_by_channel": {"wedding": 2.0},
                "max_transit_hours_by_channel": {"wedding": 120},
                "compensation": {"major_excursion_refund_ratio": 0.05}},
            note="秋季放宽")
        again = self.app.ordering.public_order("ORD-P5")
        self.assertEqual(again["rule_version_id"], "R-2026-01")
        self.assertEqual(
            again["lines"][0]["promise"]["arrival_window"], original_window)
        self.assertEqual(
            again["lines"][0]["resolved_params"]["BQ-1"]
            ["compensation"]["major_excursion_refund_ratio"],
            original_params["compensation"]["major_excursion_refund_ratio"])

    def test_q10_model_higher_temp_consumes_more_budget(self):
        params = {"q10": 2.5, "reference_temp_c": 20.0}
        cold = coldlife.consumed_hours([(2.0, 10)], **params)
        hot = coldlife.consumed_hours([(22.0, 10)], **params)
        self.assertGreater(hot, cold * 2)
        # 20℃ 每小时消耗 1 个当量小时
        self.assertAlmostEqual(
            coldlife.consumed_hours([(20.0, 10)], **params), 10.0)


if __name__ == "__main__":
    unittest.main()
