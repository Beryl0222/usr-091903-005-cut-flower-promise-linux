"""领域模型：从种苗到花束的来源档案。

每一束花在创建时即固化上游来源（种苗批次、种植棚、采切班次、采后处理），
后续的拼箱、拆箱、换车只是追加位置/容器归属事件，不改变来源档案。
"""

from .clock import now_iso

# 销售目的地类型
CHANNEL_MARKET = "wholesale_market"  # 批发市场
CHANNEL_WEDDING = "wedding"  # 婚礼现场
CHANNEL_FLORIST = "florist"  # 城市花店


class SeedBatch:
    """种苗批次（品种的源头）。"""

    def __init__(self, batch_id, cultivar, supplier=None, planted_at=None, extra=None):
        self.batch_id = batch_id
        self.cultivar = cultivar  # 品种名，如 "卡罗拉玫瑰"
        self.supplier = supplier
        self.planted_at = planted_at
        self.extra = extra or {}

    def to_dict(self):
        return {
            "batch_id": self.batch_id,
            "cultivar": self.cultivar,
            "supplier": self.supplier,
            "planted_at": self.planted_at,
            "extra": self.extra,
        }


class Greenhouse:
    """种植棚。"""

    def __init__(self, house_id, name=None, location=None, grower_id=None):
        self.house_id = house_id
        self.name = name or house_id
        self.location = location
        self.grower_id = grower_id

    def to_dict(self):
        return {"house_id": self.house_id, "name": self.name,
                "location": self.location, "grower_id": self.grower_id}


class HarvestShift:
    """采切班次：哪个班次、何时采切、采切成熟度（1-5 级）。"""

    def __init__(self, shift_id, house_id, cut_at, maturity_stage, supervisor=None):
        self.shift_id = shift_id
        self.house_id = house_id
        self.cut_at = cut_at
        self.maturity_stage = maturity_stage
        self.supervisor = supervisor

    def to_dict(self):
        return {
            "shift_id": self.shift_id,
            "house_id": self.house_id,
            "cut_at": self.cut_at,
            "maturity_stage": self.maturity_stage,
            "supervisor": self.supervisor,
        }


class PostHarvestRecord:
    """采后处理记录：预冷开始/结束、保鲜剂、等待时长由时间线推导。"""

    def __init__(self, record_id, shift_id, precool_started_at, precool_ended_at,
                 preservative=None, handled_by=None, notes=None):
        self.record_id = record_id
        self.shift_id = shift_id
        self.precool_started_at = precool_started_at
        self.precool_ended_at = precool_ended_at
        self.preservative = preservative
        self.handled_by = handled_by
        self.notes = notes

    def to_dict(self):
        return {
            "record_id": self.record_id,
            "shift_id": self.shift_id,
            "precool_started_at": self.precool_started_at,
            "precool_ended_at": self.precool_ended_at,
            "preservative": self.preservative,
            "handled_by": self.handled_by,
            "notes": self.notes,
        }


class Bouquet:
    """花束：来源不可变的最小可承诺/可结算单位。"""

    def __init__(self, bouquet_id, seed_batch_id, house_id, shift_id, post_harvest_id,
                 stems=1, created_at=None):
        self.bouquet_id = bouquet_id
        self.seed_batch_id = seed_batch_id
        self.house_id = house_id
        self.shift_id = shift_id
        self.post_harvest_id = post_harvest_id
        self.stems = stems
        self.created_at = created_at or now_iso()

    def source_ref(self):
        return {
            "bouquet_id": self.bouquet_id,
            "seed_batch_id": self.seed_batch_id,
            "house_id": self.house_id,
            "shift_id": self.shift_id,
            "post_harvest_id": self.post_harvest_id,
        }

    def to_dict(self):
        data = self.source_ref()
        data["stems"] = self.stems
        data["created_at"] = self.created_at
        return data


class ColdContainer:
    """冷链载具：拼箱/冷藏车。

    载具之间可形成父子关系：花束装入箱，箱装车；换车即把箱从旧车移到新车，
    系统记录每次移动事件，任何时刻都能回答“这束花当时在哪、跟谁同箱”。
    """

    def __init__(self, container_id, kind, capacity=None):
        self.container_id = container_id
        self.kind = kind  # "box" 拼箱 | "vehicle" 冷链车 | "cool_room" 冷库
        self.capacity = capacity

    def to_dict(self):
        return {"container_id": self.container_id, "kind": self.kind, "capacity": self.capacity}
