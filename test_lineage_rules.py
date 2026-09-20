"""溯源与规则版本化测试：来源不可变、拼箱拆箱换车可回放、旧订单不受新规则影响。"""

import unittest

from freshkeep.clock import parse_iso
from freshkeep.lineage import LineageError
from freshkeep.rules import RuleError

from test_support import build_app, pack_and_load


class ProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.app = build_app()

    def test_bouquet_inherits_full_source_chain(self):
        prov = self.app.lineage.provenance("BQ-1")
        self.assertEqual(prov["seed_batch"]["cultivar"], "卡罗拉玫瑰")
        self.assertEqual(prov["greenhouse"]["house_id"], "GH-3")
        self.assertEqual(prov["greenhouse"]["grower_id"], "G-100")
        self.assertEqual(prov["harvest_shift"]["shift_id"], "SH-0501-AM")
        self.assertEqual(prov["harvest_shift"]["maturity_stage"], 3)
        self.assertEqual(prov["post_harvest"]["record_id"], "PH-0501-AM")
        self.assertAlmostEqual(prov["derived"]["precool_wait_minutes"], 30.0)
        self.assertAlmostEqual(prov["derived"]["precool_minutes"], 120.0)

    def test_broken_source_chain_is_rejected(self):
        # 采后记录属于别的班次时不允许建档，杜绝来源错挂
        with self.assertRaises(LineageError):
            self.app.lineage.register_bouquet(
                "BQ-BAD", "SB-ROSE", "GH-3", "SH-0501-AM", "PH-0501-PM")

    def test_pack_is_idempotent_and_lineage_queryable_at_any_time(self):
        pack_and_load(self.app)
        again = self.app.lineage.pack("EV-PACK-A", "BOX-A", ["BQ-1"],
                                      "2026-04-30T23:00:00Z")
        self.assertTrue(again["deduplicated"])
        self.assertIsNone(self.app.lineage.container_of_bouquet_at(
            "BQ-1", "2026-04-30T22:00:00Z"))
        self.assertEqual(self.app.lineage.location_of_bouquet_at(
            "BQ-1", "2026-05-01T03:00:00Z")["vehicle_id"], "CAR-1")
        self.assertEqual(self.app.lineage.location_of_bouquet_at(
            "BQ-1", "2026-05-01T09:00:00Z")["vehicle_id"], "CAR-2")

    def test_unpack_and_transfer_keep_source_and_recontain(self):
        pack_and_load(self.app)
        # 拆箱后 BQ-3 不再属于 BOX-A，来源档案不变
        self.app.lineage.unpack("EV-UNPACK-1", "BOX-A", ["BQ-3"],
                                "2026-05-01T10:00:00Z")
        self.assertIsNone(self.app.lineage.container_of_bouquet_at(
            "BQ-3", "2026-05-01T11:00:00Z"))
        prov = self.app.lineage.provenance("BQ-3")
        self.assertEqual(prov["bouquet"]["shift_id"], "SH-0501-AM")
        # BQ-1 仍在换后的车上
        self.assertEqual(self.app.lineage.location_of_bouquet_at(
            "BQ-1", "2026-05-01T11:00:00Z")["vehicle_id"], "CAR-2")

    def test_affected_bouquets_during_window_excludes_unpacked_and_other_box(self):
        pack_and_load(self.app)
        self.app.lineage.unpack("EV-UNPACK-1", "BOX-A", ["BQ-3"],
                                "2026-05-01T06:30:00Z")
        hit = self.app.lineage.bouquets_in_container_during(
            "CAR-2", "2026-05-01T06:45:00Z", "2026-05-01T07:30:00Z")
        self.assertEqual(set(hit), {"BQ-1", "BQ-2"})
        # 拆箱之后的窗口不含 BQ-3
        hit_late = self.app.lineage.bouquets_in_container_during(
            "BOX-A", "2026-05-01T09:00:00Z", "2026-05-01T10:00:00Z")
        self.assertEqual(set(hit_late), {"BQ-1", "BQ-2"})
        # 换车前在 CAR-1 的窗口，三束花都在箱内
        hit_early = self.app.lineage.bouquets_in_container_during(
            "CAR-1", "2026-05-01T02:00:00Z", "2026-05-01T03:00:00Z")
        self.assertEqual(set(hit_early), {"BQ-1", "BQ-2", "BQ-3"})


class RuleVersioningTest(unittest.TestCase):
    def setUp(self):
        self.app = build_app()

    def test_no_rule_before_first_effective_version(self):
        from freshkeep.app import FreshKeepApp
        app = FreshKeepApp()
        with self.assertRaises(RuleError):
            app.rules.effective_at("2026-01-01T00:00:00Z")

    def test_effective_version_is_latest_not_later_than_order_time(self):
        self.app.rules.add_version(
            "R-2026-06", "2026-06-01T00:00:00Z",
            params_overrides={"min_vase_days_by_channel": {"wedding": 3.0}},
            note="夏季放宽")
        may = self.app.rules.effective_at("2026-05-15T00:00:00Z")
        june = self.app.rules.effective_at("2026-06-15T00:00:00Z")
        self.assertEqual(may["version_id"], "R-2026-01")
        self.assertEqual(june["version_id"], "R-2026-06")
        self.assertEqual(
            may["params"]["min_vase_days_by_channel"]["wedding"], 5.0)

    def test_cultivar_override_isolated_per_cultivar(self):
        self.app.rules.add_version(
            "R-LILY", "2026-03-01T00:00:00Z",
            cultivar_overrides={"香水百合": {"base_vase_days_by_maturity": {"3": 12.0}}})
        snap = self.app.rules.effective_at("2026-05-01T00:00:00Z")
        lily = self.app.rules.resolve_params(snap, "香水百合")
        rose = self.app.rules.resolve_params(snap, "卡罗拉玫瑰")
        self.assertEqual(lily["base_vase_days_by_maturity"]["3"], 12.0)
        self.assertEqual(rose["base_vase_days_by_maturity"]["3"], 7.5)


if __name__ == "__main__":
    unittest.main()
