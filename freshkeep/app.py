"""应用门面：把各领域服务组装成一个可注入时钟的统一入口。"""

from .clock import SimClock
from .storage import Store
from .lineage import LineageService
from .rules import RuleBook
from .ordering import OrderingService
from .monitoring import TemperatureService
from .fulfillment import FulfillmentService
from .views import Views


class FreshKeepApp:
    def __init__(self, clock=None):
        self.clock = clock or SimClock()
        self.store = Store()
        self.lineage = LineageService(self.store, self.clock)
        self.rules = RuleBook(self.store, self.clock)
        self.ordering = OrderingService(self.store, self.lineage, self.rules, self.clock)
        self.monitoring = TemperatureService(self.store, self.lineage, self.clock)
        self.fulfillment = FulfillmentService(
            self.store, self.lineage, self.monitoring, self.ordering, self.clock)
        self.views = Views(self.store, self.lineage, self.rules, self.ordering,
                           self.fulfillment, self.monitoring)

    # 常用组合动作 ---------------------------------------------------------

    def seed_demo_rules(self):
        """写入一条在很早之前生效的默认规则，便于联调。"""
        return self.rules.add_version(
            "R-2026-01", "2026-01-01T00:00:00Z",
            note="年初基线品质规则",
        )
