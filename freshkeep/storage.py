"""内存存储与只追加的决定日志。

决定日志（decision log）是管理层“回放一张订单完整决定过程”的依据：
任何对外产生结论的动作（接单承诺、异常圈定、补证、裁定、结算）都写一条
不可变记录，包含当时使用的规则版本、输入摘要与结论。
"""

import threading
from collections import deque

from .clock import now_iso


class Store:
    def __init__(self):
        self._lock = threading.RLock()
        self.seed_batches = {}
        self.greenhouses = {}
        self.shifts = {}
        self.post_harvest = {}
        self.bouquets = {}
        self.containers = {}
        # 谱系事件：{event_id: dict}，按 event_id 幂等
        self.lineage_events = {}
        self.lineage_order = deque()
        # 容器温度记录仪绑定：container_id -> logger_id
        self.logger_bindings = {}
        # 温度读数：logger_id -> {seq/read_at: reading}，天然去重
        self.temp_readings = {}
        self.temp_gaps = {}  # logger_id -> {gap_id: gap}
        # 订单与行项目
        self.orders = {}
        self.order_lines = {}  # order_id -> {line_id: line}
        # 承运回调：delivery_event_id 幂等
        self.delivery_events = {}
        # 签收回执：receipt_id 幂等；同一行可分批
        self.receipts = {}
        # 异常事件
        self.incidents = {}
        # 理赔单（每行至多一个有效理赔单）
        self.claims = {}
        self.claim_by_line = {}
        # 结算记录：line_id -> settlement（只允许一次）
        self.settlements = {}
        # 规则版本
        self.rule_versions = {}
        self.rule_order = deque()
        # 只追加决定日志
        self.decision_log = deque()
        self._seq = 0

    def lock(self):
        return self._lock

    def next_seq(self):
        with self._lock:
            self._seq += 1
            return self._seq

    def add_decision(self, kind, order_id, summary, inputs=None, outputs=None,
                     rule_version=None, at=None, actor="system"):
        entry = {
            "seq": self.next_seq(),
            "at": at or now_iso(),
            "actor": actor,
            "kind": kind,
            "order_id": order_id,
            "rule_version": rule_version,
            "summary": summary,
            "inputs": inputs or {},
            "outputs": outputs or {},
        }
        self.decision_log.append(entry)
        return entry

    def decisions_for_order(self, order_id):
        with self._lock:
            return [dict(d) for d in self.decision_log if d["order_id"] == order_id]
