"""测试夹具：构造一个时钟可控的完整应用，并预置来源档案。"""

from freshkeep.app import FreshKeepApp
from freshkeep.clock import SimClock

T0 = "2026-05-01T00:00:00Z"
RULE_V1 = "R-2026-01"


def build_app(start=T0):
    clk = SimClock(start)
    app = FreshKeepApp(clk)
    app.seed_demo_rules()
    # 种苗：玫瑰与百合（百合可做品种覆盖演示）
    app.lineage.register_seed_batch("SB-ROSE", "卡罗拉玫瑰", supplier="玉溪种苗A",
                                    planted_at="2025-11-01T00:00:00Z")
    app.lineage.register_seed_batch("SB-LILY", "香水百合", supplier="玉溪种苗B")
    app.lineage.register_greenhouse("GH-3", name="3号棚", location="玉溪红塔", grower_id="G-100")
    app.lineage.register_greenhouse("GH-7", name="7号棚", location="玉溪江川", grower_id="G-200")
    # 3号棚清晨班次，成熟度 3 级，采切后 30 分钟进预冷
    app.lineage.register_shift("SH-0501-AM", "GH-3", "2026-04-30T20:00:00Z", 3,
                               supervisor="李班")
    app.lineage.register_post_harvest(
        "PH-0501-AM", "SH-0501-AM",
        "2026-04-30T20:30:00Z", "2026-04-30T22:30:00Z",
        preservative="花之寿200", handled_by="王处理")
    # 7号棚另一批次（成熟度 4），预冷等待 3 小时——用于种植端扣款演示
    app.lineage.register_shift("SH-0501-PM", "GH-7", "2026-04-30T21:00:00Z", 4)
    app.lineage.register_post_harvest(
        "PH-0501-PM", "SH-0501-PM",
        "2026-05-01T00:00:00Z", "2026-05-01T02:00:00Z",
        preservative="花之寿200")
    for bid in ("BQ-1", "BQ-2", "BQ-3", "BQ-4", "BQ-5"):
        app.lineage.register_bouquet(bid, "SB-ROSE", "GH-3", "SH-0501-AM", "PH-0501-AM")
    app.lineage.register_bouquet("BQ-9", "SB-ROSE", "GH-7", "SH-0501-PM", "PH-0501-PM")
    # 载具
    app.lineage.register_container("BOX-A", "box")
    app.lineage.register_container("BOX-B", "box")
    app.lineage.register_container("BOX-C", "box")
    app.lineage.register_container("CAR-1", "vehicle")
    app.lineage.register_container("CAR-2", "vehicle")
    app.lineage.register_container("CAR-3", "vehicle")
    return app


def pack_and_load(app):
    """BQ-1/2/3 拼入 BOX-A 上 CAR-1，后换 CAR-2。"""
    app.lineage.pack("EV-PACK-A", "BOX-A", ["BQ-1", "BQ-2", "BQ-3"],
                     "2026-04-30T23:00:00Z")
    app.lineage.load("EV-LOAD-1", "BOX-A", "CAR-1", "2026-05-01T01:00:00Z")
    app.lineage.transfer_vehicle("EV-XFER-1", "BOX-A", "CAR-1", "CAR-2",
                                 "2026-05-01T06:00:00Z")
    app.lineage.bind_logger("LOG-A", "BOX-A", "2026-04-30T23:00:00Z")
