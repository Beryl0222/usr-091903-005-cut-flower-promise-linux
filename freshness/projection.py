"""事件流 -> 当前状态的折叠（投影）。

状态本身不落盘，随时可由 EventStore 重放重建；管理层的"回放"
就是带顺序地浏览同一条事件流。
"""

# ── 事件类型常量 ──────────────────────────────────────────────
SEED_BATCH_REGISTERED = "SeedBatchRegistered"
GREENHOUSE_REGISTERED = "GreenhouseRegistered"
CUT_SHIFT_PERFORMED = "CutShiftPerformed"
BOUQUET_HARVESTED = "BouquetHarvested"
TREATMENT_RECORDED = "PostHarvestTreatmentRecorded"

LOGGER_BOUND = "LoggerBound"
BOX_PACKED = "BoxPacked"              # 拼箱
BOX_SPLIT = "BoxSplit"                # 拆箱
SHIPMENT_CREATED = "ShipmentCreated"  # 装车发运
VEHICLE_CHANGED = "VehicleChanged"    # 换冷链车
SHIPMENT_ARRIVED = "ShipmentArrived"

ORDER_ACCEPTED = "OrderAccepted"
TEMP_READING = "TemperatureReadingRecorded"
CARRIER_CALLBACK = "CarrierCallbackAccepted"
RECEIPT_CONFIRMED = "DeliveryReceiptConfirmed"

BOUQUETS_FLAGGED = "BouquetsFlagged"
FLAG_UPDATED = "FlagUpdated"
FLAG_CLEARED = "FlagCleared"
EVIDENCE_SUBMITTED = "EvidenceSubmitted"
FLAG_RULED = "FlagRuled"

RULE_PUBLISHED = "QualityRulePublished"
SETTLEMENT_CREATED = "SettlementCreated"


class State:
    def __init__(self):
        self.seed_batches = {}
        self.greenhouses = {}
        self.shifts = {}
        self.bouquets = {}
        self.treatments = {}
        self.loggers = {}
        self.boxes = {}
        self.shipments = {}
        self.orders = {}
        self.flags = {}
        self.settlements = {}
        self.rule_versions = []
        # 去重索引
        self.command_keys = set()
        self.reading_keys = set()
        self.callback_keys = set()
        self.received_bouquet_keys = set()
        # 规则结果索引：(order_id, reason, shipment_id, win_from, win_to) -> flag_id
        self.flag_index = {}

    # ── 查询辅助 ─────────────────────────────────────────────
    def bouquet(self, bouquet_id):
        try:
            return self.bouquets[bouquet_id]
        except KeyError:
            from .errors import NotFound
            raise NotFound(f"花束不存在: {bouquet_id}")

    def order(self, order_id):
        try:
            return self.orders[order_id]
        except KeyError:
            from .errors import NotFound
            raise NotFound(f"订单不存在: {order_id}")

    def logger_readings(self, logger_id):
        logger = self.loggers.get(logger_id)
        if not logger:
            return []
        return sorted(logger["readings"].values(), key=lambda r: r["at"])

    def logger_binding_at(self, logger_id, at):
        """某时刻记录器绑在哪个实体上（箱/车）。"""
        logger = self.loggers.get(logger_id)
        if not logger:
            return None
        for binding in logger["bindings"]:
            if binding["from_at"] <= at and (binding["to_at"] is None or at < binding["to_at"]):
                return binding
        return None

    def box_logger_at(self, box_id, at):
        for logger_id, logger in self.loggers.items():
            binding = self.logger_binding_at(logger_id, at)
            if binding and binding["target_type"] == "box" and binding["target_id"] == box_id:
                return logger_id
        return None

    def vehicle_logger_at(self, shipment_id, at):
        shipment = self.shipments.get(shipment_id)
        if not shipment:
            return None
        for leg in shipment["legs"]:
            if leg["from_at"] <= at and (leg["to_at"] is None or at < leg["to_at"]):
                return leg.get("logger_id")
        return None

    def bouquets_in_shipment(self, shipment_id):
        shipment = self.shipments.get(shipment_id)
        if not shipment:
            return []
        result = []
        for box_id in shipment["box_ids"]:
            box = self.boxes.get(box_id)
            if box:
                result.extend(sorted(box["bouquet_ids"]))
        return result


def fold(events, state=None):
    """把事件依次折叠进状态。"""
    state = state or State()
    for event in events:
        apply_event(state, event)
    return state


def apply_event(state, event):
    etype = event["type"]
    at = event["at"]
    p = event["payload"]
    handler = _HANDLERS.get(etype)
    if handler is not None:
        handler(state, at, p)


# ── 各事件投影器 ──────────────────────────────────────────────
def _on_seed_batch(state, at, p):
    state.seed_batches[p["seed_batch_id"]] = {
        "id": p["seed_batch_id"],
        "cultivar": p.get("cultivar"),
        "supplier": p.get("supplier"),
        "planted_at": p.get("planted_at"),
    }


def _on_greenhouse(state, at, p):
    state.greenhouses[p["greenhouse_id"]] = {
        "id": p["greenhouse_id"],
        "name": p.get("name", p["greenhouse_id"]),
    }


def _on_shift(state, at, p):
    state.shifts[p["shift_id"]] = {
        "id": p["shift_id"],
        "greenhouse_id": p["greenhouse_id"],
        "seed_batch_id": p["seed_batch_id"],
        "cut_date": p.get("cut_date", at[:10]),
        "shift_code": p.get("shift_code", ""),
    }


def _on_bouquet_harvested(state, at, p):
    bouquet = {
        "id": p["bouquet_id"],
        "cultivar": p["cultivar"],
        "shift_id": p["shift_id"],
        "seed_batch_id": p["seed_batch_id"],
        "greenhouse_id": p["greenhouse_id"],
        "maturity": p.get("maturity", "commercial"),
        "cut_at": p["cut_at"],
        "stem_count": p.get("stem_count", 20),
        "treatment_ids": [],
        "precool_wait_min": 0,
        "grading": None,
        "chain": [],          # 位置链：采后 → 箱 → 车 → 签收
        "current_box": None,
        "current_shipment": None,
        "received_at": None,
        "receipt_condition": None,
    }
    state.bouquets[p["bouquet_id"]] = bouquet
    # 继承链的第一环
    bouquet["chain"].append({"at": p["cut_at"], "kind": "harvested"})


def _on_treatment(state, at, p):
    record = {
        "id": p["record_id"],
        "bouquet_ids": list(p["bouquet_ids"]),
        "at": at,
        "precool_wait_min": p.get("precool_wait_min", 0),
        "pulse_solution": p.get("pulse_solution"),
        "grading": p.get("grading"),
        "note": p.get("note", ""),
    }
    state.treatments[p["record_id"]] = record
    for bid in p["bouquet_ids"]:
        bouquet = state.bouquets[bid]
        bouquet["treatment_ids"].append(p["record_id"])
        # 预冷等待以采后记录为准（取最早一条的等待时长）
        if record["precool_wait_min"] > bouquet["precool_wait_min"]:
            bouquet["precool_wait_min"] = record["precool_wait_min"]
        if p.get("grading"):
            bouquet["grading"] = p["grading"]
        bouquet["chain"].append({"at": at, "kind": "treated", "record_id": p["record_id"]})


def _on_logger_bound(state, at, p):
    logger = state.loggers.setdefault(
        p["logger_id"], {"id": p["logger_id"], "bindings": [], "readings": {}}
    )
    # 关闭该记录器此前未关闭的绑定
    for binding in logger["bindings"]:
        if binding["to_at"] is None:
            binding["to_at"] = at
    logger["bindings"].append(
        {
            "target_type": p["target_type"],   # box / vehicle
            "target_id": p["target_id"],
            "from_at": at,
            "to_at": None,
        }
    )


def _append_chain(state, bid, entry):
    state.bouquets[bid]["chain"].append(entry)


def _on_box_packed(state, at, p):
    box = state.boxes.setdefault(
        p["box_id"], {"id": p["box_id"], "created_at": at, "bouquet_ids": [], "history": []}
    )
    new_ids = [bid for bid in p["bouquet_ids"] if bid not in box["bouquet_ids"]]
    box["bouquet_ids"].extend(new_ids)
    box["history"].append({"at": at, "type": "packed", "bouquet_ids": list(p["bouquet_ids"])})
    for bid in p["bouquet_ids"]:
        bouquet = state.bouquets[bid]
        bouquet["current_box"] = p["box_id"]
        _append_chain(state, bid, {"at": at, "kind": "packed", "box_id": p["box_id"]})


def _on_box_split(state, at, p):
    source = state.boxes[p["from_box_id"]]
    for piece in p["splits"]:
        new_box = state.boxes.setdefault(
            piece["box_id"],
            {"id": piece["box_id"], "created_at": at, "bouquet_ids": [], "history": []},
        )
        for bid in piece["bouquet_ids"]:
            if bid not in source["bouquet_ids"]:
                from .errors import Conflict
                raise Conflict(f"花束 {bid} 不在箱 {p['from_box_id']} 中，无法拆出")
            source["bouquet_ids"].remove(bid)
            new_box["bouquet_ids"].append(bid)
            bouquet = state.bouquets[bid]
            bouquet["current_box"] = piece["box_id"]
            _append_chain(
                state,
                bid,
                {"at": at, "kind": "split", "from_box_id": p["from_box_id"], "box_id": piece["box_id"]},
            )
        new_box["history"].append(
            {"at": at, "type": "split_in", "from_box_id": p["from_box_id"], "bouquet_ids": list(piece["bouquet_ids"])}
        )
    source["history"].append(
        {"at": at, "type": "split_out", "splits": [dict(s) for s in p["splits"]]}
    )


def _on_shipment_created(state, at, p):
    shipment = {
        "id": p["shipment_id"],
        "carrier": p["carrier"],
        "route": p.get("route", ""),
        "box_ids": list(p["box_ids"]),
        "legs": [
            {
                "vehicle_id": p["vehicle_id"],
                "logger_id": p.get("vehicle_logger_id"),
                "from_at": at,
                "to_at": None,
            }
        ],
        "departed_at": at,
        "arrived_at": None,
        "callbacks": [],
    }
    state.shipments[p["shipment_id"]] = shipment
    for box_id in p["box_ids"]:
        box = state.boxes.setdefault(
            box_id, {"id": box_id, "created_at": at, "bouquet_ids": [], "history": []}
        )
        box["history"].append({"at": at, "type": "loaded", "shipment_id": p["shipment_id"]})
        for bid in box["bouquet_ids"]:
            bouquet = state.bouquets[bid]
            bouquet["current_shipment"] = p["shipment_id"]
            _append_chain(
                state,
                bid,
                {"at": at, "kind": "loaded", "shipment_id": p["shipment_id"], "vehicle_id": p["vehicle_id"]},
            )


def _on_vehicle_changed(state, at, p):
    shipment = state.shipments[p["shipment_id"]]
    for leg in shipment["legs"]:
        if leg["to_at"] is None:
            leg["to_at"] = at
    shipment["legs"].append(
        {
            "vehicle_id": p["to_vehicle_id"],
            "logger_id": p.get("to_vehicle_logger_id"),
            "from_at": at,
            "to_at": None,
        }
    )
    for bid in state.bouquets_in_shipment(p["shipment_id"]):
        _append_chain(
            state,
            bid,
            {
                "at": at,
                "kind": "vehicle_changed",
                "shipment_id": p["shipment_id"],
                "vehicle_id": p["to_vehicle_id"],
            },
        )


def _on_shipment_arrived(state, at, p):
    shipment = state.shipments[p["shipment_id"]]
    shipment["arrived_at"] = p["arrived_at"]
    for bid in state.bouquets_in_shipment(p["shipment_id"]):
        _append_chain(state, bid, {"at": p["arrived_at"], "kind": "shipment_arrived"})


def _on_order_accepted(state, at, p):
    order = {
        "id": p["order_id"],
        "channel": p.get("channel", "wholesale"),
        "customer": p.get("customer", ""),
        "destination": p.get("destination", ""),
        "amount": p["amount"],
        "bouquet_ids": list(p["bouquet_ids"]),
        "accepted_at": p["accepted_at"],
        "rule_version": p["rule_version"],
        "rule_snapshot": p["rule_snapshot"],
        "arrival_window": p["arrival_window"],
        "commitments": {c["bouquet_id"]: c for c in p["commitments"]},
        "status": "accepted",
        "receipts": [],
        "received_at": None,
    }
    state.orders[p["order_id"]] = order
    if p.get("idempotency_key"):
        state.command_keys.add(p["idempotency_key"])


def _on_temp_reading(state, at, p):
    logger = state.loggers.setdefault(
        p["logger_id"], {"id": p["logger_id"], "bindings": [], "readings": {}}
    )
    key = (p["logger_id"], p["at"])
    state.reading_keys.add(key)
    logger["readings"][p["at"]] = {
        "at": p["at"],
        "temp_c": float(p["temp_c"]),
        "received_at": p.get("received_at", at),
    }


def _on_carrier_callback(state, at, p):
    state.callback_keys.add(p["idempotency_key"])
    shipment = state.shipments.get(p["shipment_id"])
    if shipment is not None:
        shipment["callbacks"].append(
            {
                "key": p["idempotency_key"],
                "status": p["status"],
                "occurred_at": p["occurred_at"],
                "recorded_at": at,
            }
        )


def _on_receipt(state, at, p):
    order = state.orders[p["order_id"]]
    order["receipts"].append(
        {
            "receipt_id": p["receipt_id"],
            "shipment_id": p.get("shipment_id"),
            "bouquet_ids": list(p["bouquet_ids"]),
            "received_at": p["received_at"],
            "condition": p.get("condition", "normal"),
            "note": p.get("note", ""),
        }
    )
    for bid in p["bouquet_ids"]:
        key = (p["order_id"], bid)
        state.received_bouquet_keys.add(key)
        bouquet = state.bouquets[bid]
        bouquet["received_at"] = p["received_at"]
        bouquet["receipt_condition"] = p.get("condition", "normal")
        _append_chain(state, bid, {"at": p["received_at"], "kind": "received", "order_id": p["order_id"]})
    all_received = all(
        (p["order_id"], bid) in state.received_bouquet_keys for bid in order["bouquet_ids"]
    )
    order["received_at"] = max(
        [r["received_at"] for r in order["receipts"]],
        default=None,
    )
    order["status"] = "received" if all_received else "partial_received"


def _on_flagged(state, at, p):
    flag = {
        "id": p["flag_id"],
        "order_id": p["order_id"],
        "shipment_id": p.get("shipment_id"),
        "reason": p["reason"],
        "status": "open",
        "bouquet_ids": list(p["bouquet_ids"]),
        "window": p.get("window"),
        "detail": p.get("detail", ""),
        "source": p.get("source", "system"),
        "opened_at": at,
        "history": [{"at": at, "action": "opened", "detail": p.get("detail", "")}],
        "evidence": [],
        "liable_party": None,
        "resolution": None,
    }
    state.flags[p["flag_id"]] = flag
    state.flag_index[_flag_key(flag)] = p["flag_id"]


def _flag_key(flag):
    window = flag.get("window") or {}
    # 同一订单/同一原因/同一车次的异常只圈一次；窗口终点会随补传延展。
    return (
        flag["order_id"],
        flag["reason"],
        flag.get("shipment_id"),
        window.get("from"),
    )


def _on_flag_updated(state, at, p):
    flag = state.flags[p["flag_id"]]
    flag["bouquet_ids"] = list(p["bouquet_ids"])
    if p.get("window"):
        flag["window"] = p["window"]
    if p.get("detail"):
        flag["detail"] = p["detail"]
    flag["history"].append({"at": at, "action": "updated", "detail": p.get("detail", "")})


def _on_flag_cleared(state, at, p):
    flag = state.flags[p["flag_id"]]
    flag["status"] = "cleared"
    flag["resolution"] = "cleared"
    flag["history"].append({"at": at, "action": "cleared", "detail": p.get("reason", "")})
    state.flag_index.pop(_flag_key(flag), None)


def _on_evidence(state, at, p):
    flag = state.flags[p["flag_id"]]
    flag["evidence"].append(
        {
            "evidence_id": p["evidence_id"],
            "party": p["party"],
            "kind": p.get("kind", "note"),
            "note": p.get("note", ""),
            "submitted_at": at,
        }
    )
    flag["status"] = "evidence_received"
    flag["history"].append(
        {"at": at, "action": "evidence", "party": p["party"], "detail": p.get("note", "")}
    )


def _on_flag_ruled(state, at, p):
    flag = state.flags[p["flag_id"]]
    flag["status"] = "ruled"
    flag["liable_party"] = p["liable_party"]
    flag["resolution"] = p["resolution"]
    flag["history"].append({"at": at, "action": "ruled", "detail": p.get("reason", "")})


def _on_rule_published(state, at, p):
    state.rule_versions.append(p["version_record"])


def _on_settlement(state, at, p):
    state.settlements[p["order_id"]] = p
    order = state.orders[p["order_id"]]
    order["status"] = "settled"


_HANDLERS = {
    SEED_BATCH_REGISTERED: _on_seed_batch,
    GREENHOUSE_REGISTERED: _on_greenhouse,
    CUT_SHIFT_PERFORMED: _on_shift,
    BOUQUET_HARVESTED: _on_bouquet_harvested,
    TREATMENT_RECORDED: _on_treatment,
    LOGGER_BOUND: _on_logger_bound,
    BOX_PACKED: _on_box_packed,
    BOX_SPLIT: _on_box_split,
    SHIPMENT_CREATED: _on_shipment_created,
    VEHICLE_CHANGED: _on_vehicle_changed,
    SHIPMENT_ARRIVED: _on_shipment_arrived,
    ORDER_ACCEPTED: _on_order_accepted,
    TEMP_READING: _on_temp_reading,
    CARRIER_CALLBACK: _on_carrier_callback,
    RECEIPT_CONFIRMED: _on_receipt,
    BOUQUETS_FLAGGED: _on_flagged,
    FLAG_UPDATED: _on_flag_updated,
    FLAG_CLEARED: _on_flag_cleared,
    EVIDENCE_SUBMITTED: _on_evidence,
    FLAG_RULED: _on_flag_ruled,
    RULE_PUBLISHED: _on_rule_published,
    SETTLEMENT_CREATED: _on_settlement,
}
