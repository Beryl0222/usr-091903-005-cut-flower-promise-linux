"""应用服务：命令处理、幂等、异常自动圈定与重评、结算。

所有写操作都遵循同一形态：
    校验 -> 追加事件 -> 折叠 -> 触发反应（可能再追加事件）
事件一旦写入不可修改；补传/补证通过"新事件 + 重评"改变结论。
"""

import uuid
from collections import defaultdict
from copy import deepcopy

from . import clock as timeutil
from . import coldchain
from . import projection as P
from .errors import Conflict, NotFound, RuleError, SettlementClosed, ValidationFailed
from .rules import RuleBook, band_for_temp, budget_rate, compensation_ratio
from .store import EventStore

GAP = coldchain.READING_GAP_MIN


def _new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class FreshnessService:
    def __init__(self, store=None, clock=None, rulebook=None):
        self.store = store or EventStore()
        self.clock = clock
        self.state = P.fold(self.store.all())
        self.rulebook = rulebook or RuleBook()
        # 从事件流重建时，把已发布的自定义规则回填到规则簿（按生效时间排序）
        known = {v["version"] for v in self.rulebook.all_versions()}
        missing = [v for v in self.state.rule_versions if v["version"] not in known]
        for version in sorted(missing, key=lambda v: v["effective_from"]):
            self.rulebook._versions.append(deepcopy(version))
        if missing:
            self.rulebook.normalize_windows()
        # 内置版本登记到投影索引（内存比对用，不写事件）
        projected = {v["version"] for v in self.state.rule_versions}
        for version in self.rulebook.all_versions():
            if version["version"] not in projected:
                self.state.rule_versions.append(version)

    # ── 内部基础件 ─────────────────────────────────────────────
    def _at(self, at):
        return at or timeutil.now(self.clock)

    def _append(self, etype, payload, at, actor=None):
        event = self.store.append(etype, payload, at, actor=actor)
        P.apply_event(self.state, event)
        return event

    def _require_command_once(self, key):
        if key and key in self.state.command_keys:
            raise Conflict("重复的命令请求", idempotency_key=key)
        return key

    def _bouquets(self, bouquet_ids):
        return [self.state.bouquet(bid) for bid in bouquet_ids]

    # ── 1. 产地建档：种苗批次 / 棚 / 采切班次 ─────────────────
    def register_seed_batch(self, seed_batch_id, cultivar, supplier=None, planted_at=None, at=None):
        if seed_batch_id in self.state.seed_batches:
            raise Conflict("种苗批次已登记", seed_batch_id=seed_batch_id)
        self._append(
            P.SEED_BATCH_REGISTERED,
            {
                "seed_batch_id": seed_batch_id,
                "cultivar": cultivar,
                "supplier": supplier,
                "planted_at": planted_at,
            },
            self._at(at),
            actor="grower",
        )
        return seed_batch_id

    def register_greenhouse(self, greenhouse_id, name=None, at=None):
        if greenhouse_id in self.state.greenhouses:
            raise Conflict("种植棚已登记", greenhouse_id=greenhouse_id)
        self._append(
            P.GREENHOUSE_REGISTERED,
            {"greenhouse_id": greenhouse_id, "name": name or greenhouse_id},
            self._at(at),
            actor="grower",
        )
        return greenhouse_id

    def perform_cut_shift(self, shift_id, greenhouse_id, seed_batch_id, shift_code=None, at=None):
        if shift_id in self.state.shifts:
            raise Conflict("采切班次已登记", shift_id=shift_id)
        if greenhouse_id not in self.state.greenhouses:
            raise NotFound("种植棚不存在", greenhouse_id=greenhouse_id)
        if seed_batch_id not in self.state.seed_batches:
            raise NotFound("种苗批次不存在", seed_batch_id=seed_batch_id)
        at = self._at(at)
        self._append(
            P.CUT_SHIFT_PERFORMED,
            {
                "shift_id": shift_id,
                "greenhouse_id": greenhouse_id,
                "seed_batch_id": seed_batch_id,
                "cut_date": at[:10],
                "shift_code": shift_code or "",
            },
            at,
            actor="grower",
        )
        return shift_id

    def harvest_bouquets(self, shift_id, bouquet_ids, maturity="commercial", stem_count=20, at=None):
        """一束花在采切时即继承：种苗批次、种植棚、采切班次。"""
        shift = self.state.shifts.get(shift_id)
        if not shift:
            raise NotFound("采切班次不存在", shift_id=shift_id)
        at = self._at(at)
        for bid in bouquet_ids:
            if bid in self.state.bouquets:
                raise Conflict("花束已采切建档", bouquet_id=bid)
        for bid in bouquet_ids:
            self._append(
                P.BOUQUET_HARVESTED,
                {
                    "bouquet_id": bid,
                    "cultivar": self.state.seed_batches[shift["seed_batch_id"]]["cultivar"],
                    "shift_id": shift_id,
                    "seed_batch_id": shift["seed_batch_id"],
                    "greenhouse_id": shift["greenhouse_id"],
                    "maturity": maturity,
                    "cut_at": at,
                    "stem_count": stem_count,
                },
                at,
                actor="grower",
            )
        return list(bouquet_ids)

    def record_postharvest(self, bouquet_ids, precool_wait_min=0, pulse_solution=None,
                           grading=None, note="", record_id=None, at=None):
        """采后处理记录：预冷等待、保鲜剂脉冲、分级。"""
        at = self._at(at)
        record_id = record_id or _new_id("treat")
        self._bouquets(bouquet_ids)
        self._append(
            P.TREATMENT_RECORDED,
            {
                "record_id": record_id,
                "bouquet_ids": list(bouquet_ids),
                "precool_wait_min": precool_wait_min,
                "pulse_solution": pulse_solution,
                "grading": grading,
                "note": note,
            },
            at,
            actor="postharvest",
        )
        return record_id

    # ── 2. 拼箱 / 拆箱 / 记录器 / 换车 ────────────────────────
    def bind_logger(self, logger_id, target_type, target_id, at=None):
        if target_type not in ("box", "vehicle"):
            raise ValidationFailed("记录器只能绑定到 box 或 vehicle")
        at = self._at(at)
        self._append(P.LOGGER_BOUND,
                     {"logger_id": logger_id, "target_type": target_type, "target_id": target_id},
                     at, actor="carrier")
        return logger_id

    def pack_box(self, box_id, bouquet_ids, logger_id=None, at=None):
        """拼箱：多来源花束同箱，各自来源链不丢失。"""
        at = self._at(at)
        if box_id in self.state.boxes:
            raise Conflict("箱号已使用", box_id=box_id)
        self._bouquets(bouquet_ids)
        self._append(P.BOX_PACKED, {"box_id": box_id, "bouquet_ids": list(bouquet_ids)}, at, actor="packer")
        if logger_id:
            self._append(P.LOGGER_BOUND,
                         {"logger_id": logger_id, "target_type": "box", "target_id": box_id},
                         at, actor="packer")
        return box_id

    def split_box(self, from_box_id, splits, at=None):
        """拆箱：花束进入新箱，来源（旧箱、车次、花簿）继续保留。"""
        at = self._at(at)
        if from_box_id not in self.state.boxes:
            raise NotFound("箱不存在", box_id=from_box_id)
        for piece in splits:
            if piece["box_id"] in self.state.boxes:
                raise Conflict("新箱号已存在", box_id=piece["box_id"])
        self._append(
            P.BOX_SPLIT,
            {"from_box_id": from_box_id, "splits": [dict(s) for s in splits]},
            at,
            actor="packer",
        )
        # 注意：不自动迁移旧箱记录器——新箱需显式绑定新记录器；
        # 未绑定的在途区间按"断连保守温度"计费，等待承运方补传/补证。
        return [s["box_id"] for s in splits]

    def create_shipment(self, shipment_id, carrier, box_ids, vehicle_id,
                        vehicle_logger_id=None, route="", at=None):
        at = self._at(at)
        if shipment_id in self.state.shipments:
            raise Conflict("车次已存在", shipment_id=shipment_id)
        for box_id in box_ids:
            if box_id not in self.state.boxes:
                raise NotFound("箱不存在", box_id=box_id)
        self._append(
            P.SHIPMENT_CREATED,
            {
                "shipment_id": shipment_id,
                "carrier": carrier,
                "route": route,
                "box_ids": list(box_ids),
                "vehicle_id": vehicle_id,
                "vehicle_logger_id": vehicle_logger_id,
            },
            at,
            actor="carrier",
        )
        if vehicle_logger_id:
            self._append(P.LOGGER_BOUND,
                         {"logger_id": vehicle_logger_id, "target_type": "vehicle",
                          "target_id": vehicle_id},
                         at, actor="carrier")
        return shipment_id

    def change_vehicle(self, shipment_id, to_vehicle_id, to_vehicle_logger_id=None, at=None):
        """换冷链车：关闭旧运输段、开新段，花束来源链不断。"""
        at = self._at(at)
        if shipment_id not in self.state.shipments:
            raise NotFound("车次不存在", shipment_id=shipment_id)
        self._append(
            P.VEHICLE_CHANGED,
            {"shipment_id": shipment_id, "to_vehicle_id": to_vehicle_id,
             "to_vehicle_logger_id": to_vehicle_logger_id},
            at,
            actor="carrier",
        )
        if to_vehicle_logger_id:
            self._append(P.LOGGER_BOUND,
                         {"logger_id": to_vehicle_logger_id, "target_type": "vehicle",
                          "target_id": to_vehicle_id},
                         at, actor="carrier")
        return to_vehicle_id

    def shipment_arrived(self, shipment_id, arrived_at=None, at=None):
        at = self._at(at)
        arrived_at = arrived_at or at
        if shipment_id not in self.state.shipments:
            raise NotFound("车次不存在", shipment_id=shipment_id)
        self._append(P.SHIPMENT_ARRIVED,
                     {"shipment_id": shipment_id, "arrived_at": arrived_at}, at, actor="carrier")
        self._react_to_arrival(shipment_id, at)
        return arrived_at

    # ── 3. 温度记录：断连补传、自然键去重 ─────────────────────
    def record_temperature(self, logger_id, reading_at, temp_c, received_at=None, at=None):
        """写入一条温度读数。

        (logger_id, reading_at) 是自然键：重复推送（含承运方重试）
        不再产生第二条记录，也不会重复圈定；断连补传只是把读数
        按 reading_at 插回时间线，随后统一重算。
        """
        at = self._at(at)
        received_at = received_at or at
        key = (logger_id, reading_at)
        duplicate = key in self.state.reading_keys
        if duplicate:
            existing = self.state.loggers[logger_id]["readings"][reading_at]
            return {"duplicate": True, "logger_id": logger_id, "at": reading_at,
                    "temp_c": existing["temp_c"]}
        self._append(
            P.TEMP_READING,
            {"logger_id": logger_id, "at": reading_at, "temp_c": float(temp_c),
             "received_at": received_at},
            at,
            actor="logger",
        )
        self._react_to_reading(logger_id, reading_at, at)
        self._reevaluate_flags_for_reading(logger_id, reading_at, at)
        return {"duplicate": False, "logger_id": logger_id, "at": reading_at, "temp_c": float(temp_c)}

    def record_temperature_batch(self, readings, at=None):
        """批量补传：逐条去重，全部写入后只重评一次。"""
        at = self._at(at)
        accepted, duplicates = [], []
        for item in readings:
            key = (item["logger_id"], item["at"])
            if key in self.state.reading_keys:
                duplicates.append(item)
                continue
            self._append(
                P.TEMP_READING,
                {"logger_id": item["logger_id"], "at": item["at"],
                 "temp_c": float(item["temp_c"]),
                 "received_at": item.get("received_at", at)},
                at,
                actor="logger",
            )
            accepted.append(item)
        for item in accepted:
            self._react_to_reading(item["logger_id"], item["at"], at)
        for item in accepted:
            self._reevaluate_flags_for_reading(item["logger_id"], item["at"], at)
        return {"accepted": len(accepted), "duplicates": len(duplicates)}

    # ── 4. 接单：规则时点固化 + 冷量窗口 ─────────────────────
    def quote_order(self, bouquet_ids, at=None):
        """不出单，只返回当前可承诺窗口（用当前有效规则试算）。"""
        at = self._at(at)
        rule = self.rulebook.effective_at(at)
        return self._compute_quote(bouquet_ids, rule, at)

    def accept_order(self, order_id, bouquet_ids, amount, channel="wholesale",
                     customer="", destination="", idempotency_key=None, at=None):
        """接单：固化当时有效规则版本与每束花冷量快照。

        固化之后，该单的窗口/瓶插期/赔付档位永远按这份快照解释，
        之后规则放宽不影响旧单。
        """
        at = self._at(at)
        self._require_command_once(idempotency_key)
        if order_id in self.state.orders:
            raise Conflict("订单号已存在", order_id=order_id)
        for order in self.state.orders.values():
            overlap = set(order["bouquet_ids"]) & set(bouquet_ids)
            if overlap:
                raise Conflict("花束已在其他订单中", bouquet_ids=sorted(overlap))
        rule = self.rulebook.effective_at(at)
        quote = self._compute_quote(bouquet_ids, rule, at)
        if not quote["committable"]:
            raise RuleError("当前冷量不足，无法按品质规则承诺到货窗口",
                            order_id=order_id, reasons=quote["reject_reasons"])
        self._append(
            P.ORDER_ACCEPTED,
            {
                "order_id": order_id,
                "channel": channel,
                "customer": customer,
                "destination": destination,
                "amount": amount,
                "bouquet_ids": list(bouquet_ids),
                "accepted_at": at,
                "idempotency_key": idempotency_key,
                "rule_version": rule["version"],
                "rule_snapshot": deepcopy(rule),
                "arrival_window": quote["window"],
                "commitments": quote["commitments"],
            },
            at,
            actor="sales",
        )
        return self.get_order(order_id)

    def _compute_quote(self, bouquet_ids, rule, at):
        bouquets = self._bouquets(bouquet_ids)
        window_min = 6 * 60
        commitments = []
        reject_reasons = []
        min_hours = None
        for bouquet in bouquets:
            cool = coldchain.cool_state(self.state, rule, bouquet, at)
            remaining = cool["remaining_min"]
            # 可支撑的最长在途：剩余冷量全部按理想冷藏消耗
            supportable_min = int(remaining // budget_rate(rule, 2.0))
            max_transit_min = min(rule["max_arrival_hours"] * 60, supportable_min)
            promised_vase = max(0, remaining - window_min * budget_rate(rule, 2.0))
            if remaining < rule["min_remaining_cool_min"]:
                reject_reasons.append(
                    {"bouquet_id": bouquet["id"], "reason": "remaining_below_floor",
                     "remaining_min": remaining, "floor_min": rule["min_remaining_cool_min"]}
                )
            if max_transit_min < window_min:
                reject_reasons.append(
                    {"bouquet_id": bouquet["id"], "reason": "cannot_cover_min_window",
                     "supportable_min": supportable_min, "window_min": window_min}
                )
            commitments.append({
                "bouquet_id": bouquet["id"],
                "cultivar": bouquet["cultivar"],
                "remaining_min": remaining,
                "remaining_vase_hours": cool["remaining_vase_hours"],
                "promised_vase_hours": round(promised_vase / 60, 1),
                "supportable_transit_hours": round(max_transit_min / 60, 1),
                "budget_min": cool["budget_min"],
                "consumed_min": cool["consumed_min"],
            })
            min_hours = max_transit_min / 60 if min_hours is None else min(min_hours, max_transit_min / 60)
        max_hours = min(rule["max_arrival_hours"], int((min_hours or 0) // 6) * 6)
        window = {
            "from": timeutil.add_minutes(at, window_min),
            "until": timeutil.add_minutes(at, max_hours * 60),
            "min_hours": window_min / 60,
            "max_hours": max_hours,
        }
        return {
            "at": at,
            "rule_version": rule["version"],
            "window": window,
            "commitments": commitments,
            "committable": not reject_reasons,
            "reject_reasons": reject_reasons,
        }

    # ── 5. 承运回调：幂等键，重复回调只保留一次 ───────────────
    def carrier_callback(self, shipment_id, status, occurred_at, idempotency_key, at=None, note=""):
        at = self._at(at)
        if shipment_id not in self.state.shipments:
            raise NotFound("车次不存在", shipment_id=shipment_id)
        if idempotency_key in self.state.callback_keys:
            return {"duplicate": True, "shipment_id": shipment_id, "idempotency_key": idempotency_key}
        self._append(
            P.CARRIER_CALLBACK,
            {"shipment_id": shipment_id, "status": status, "occurred_at": occurred_at,
             "idempotency_key": idempotency_key, "note": note},
            at,
            actor="carrier",
        )
        return {"duplicate": False, "shipment_id": shipment_id, "status": status}

    # ── 6. 分批签收：每束花只签收一次 ─────────────────────────
    def confirm_receipt(self, order_id, bouquet_ids, received_at, condition="normal",
                        shipment_id=None, note="", receipt_id=None, at=None):
        """收货方分批签收。同束花重复签收被拒绝，整单可多次分批。"""
        at = self._at(at)
        order = self.state.order(order_id)
        unknown = [b for b in bouquet_ids if b not in order["bouquet_ids"]]
        if unknown:
            raise ValidationFailed("花束不属于该订单", bouquet_ids=unknown)
        already = [b for b in bouquet_ids if (order_id, b) in self.state.received_bouquet_keys]
        if already:
            raise Conflict("花束已签收，不能重复结算", bouquet_ids=already)
        receipt_id = receipt_id or _new_id("rcpt")
        self._append(
            P.RECEIPT_CONFIRMED,
            {"receipt_id": receipt_id, "order_id": order_id, "shipment_id": shipment_id,
             "bouquet_ids": list(bouquet_ids), "received_at": received_at,
             "condition": condition, "note": note},
            at,
            actor="receiver",
        )
        self._react_to_receipt(order_id, at)
        return receipt_id

    # ── 7. 规则发布（新版本只对未来订单生效） ─────────────────
    def publish_rule(self, version, effective_from, content, note="", at=None):
        at = self._at(at)
        record = self.rulebook.publish(version, effective_from, content, published_at=at)
        record["note"] = note or record.get("note", "")
        self._append(P.RULE_PUBLISHED, {"version_record": record}, at, actor="management")
        return record

    # ── 8. 补证与人工裁定 ─────────────────────────────────────
    def submit_evidence(self, flag_id, party, kind="note", note="", evidence_id=None, at=None):
        """责任方对系统圈出的异常补证；补证后自动重评。"""
        at = self._at(at)
        flag = self.state.flags.get(flag_id)
        if not flag:
            raise NotFound("异常标记不存在", flag_id=flag_id)
        if flag["status"] in ("ruled", "cleared"):
            raise Conflict("异常已闭环，不能再补证", flag_id=flag_id, status=flag["status"])
        evidence_id = evidence_id or _new_id("ev")
        self._append(
            P.EVIDENCE_SUBMITTED,
            {"flag_id": flag_id, "evidence_id": evidence_id, "party": party,
             "kind": kind, "note": note},
            at,
            actor=party,
        )
        self._reevaluate_flag(flag_id, at)
        return evidence_id

    def rule_flag(self, flag_id, liable_party, resolution, reason="", at=None):
        """管理层人工终裁（补证仍无法自动消除争议时）。"""
        at = self._at(at)
        flag = self.state.flags.get(flag_id)
        if not flag:
            raise NotFound("异常标记不存在", flag_id=flag_id)
        if flag["status"] in ("ruled", "cleared"):
            raise Conflict("异常已闭环", flag_id=flag_id)
        self._append(
            P.FLAG_RULED,
            {"flag_id": flag_id, "liable_party": liable_party, "resolution": resolution,
             "reason": reason},
            at,
            actor="management",
        )
        return self.state.flags[flag_id]

    def open_flag_manual(self, order_id, reason, bouquet_ids, detail="", at=None):
        """客服/管理层人工圈定（如收货投诉）。"""
        at = self._at(at)
        self.state.order(order_id)
        self._bouquets(bouquet_ids)
        flag_id = self._open_flag(order_id, reason, bouquet_ids, None, None, detail, at, source="manual")
        return flag_id

    # ── 反应：读数 / 到达 / 签收 ───────────────────────────────
    def _react_to_reading(self, logger_id, reading_at, at):
        reading = self.state.loggers[logger_id]["readings"][reading_at]
        temp_c = reading["temp_c"]
        affected = self._bouquets_actually_measured_by(logger_id, reading_at)
        if not affected:
            return
        orders = self._orders_for_bouquets(affected, statuses=("accepted", "partial_received", "received"))
        if not orders:
            return
        for order_id, bouquet_ids in orders.items():
            rule = self.state.orders[order_id]["rule_snapshot"]
            hit = [b for b in bouquet_ids if b in affected]
            band = band_for_temp(rule, temp_c)
            if band.get("excursion"):
                window = {"from": reading_at, "to": reading_at}
                detail = f"{reading_at} 温度 {temp_c}℃ 落入超温档 {band['name']}（记录器 {logger_id}）"
                self._merge_flag(order_id, "temperature_excursion", hit,
                                 self._shipment_for(hit), window, detail, at)
        # 断连检测：新读数揭示了前段空档（相邻读数间隔过长）
        self._detect_disconnect_gaps(logger_id, reading_at, at)

    def _bouquets_actually_measured_by(self, logger_id, reading_at):
        """读数只对"当时实际采用该记录器计费"的花束生效。

        车厢记录器报高温，但若花束所在箱有自己的箱记录器（更贴近花的
        微气候、计费时优先），则不应圈定这些花束。
        """
        raw = coldchain.affected_bouquets_for_reading(self.state, logger_id, reading_at)
        result = set()
        for bid in raw:
            active_logger, _kind, _target = coldchain.resolve_logger(
                self.state, self.state.bouquets[bid], reading_at
            )
            if active_logger == logger_id:
                result.add(bid)
        return result

    def _detect_disconnect_gaps(self, logger_id, reading_at, at):
        logger = self.state.loggers[logger_id]
        timeline = sorted(logger["readings"].keys())
        idx = timeline.index(reading_at)
        gaps = []
        if idx > 0:
            prev = timeline[idx - 1]
            if timeutil.diff_minutes(reading_at, prev) > GAP:
                gaps.append((prev, reading_at))
        if idx + 1 < len(timeline):
            nxt = timeline[idx + 1]
            if timeutil.diff_minutes(nxt, reading_at) > GAP:
                gaps.append((reading_at, nxt))
        for gap_from, gap_to in gaps:
            midpoint = timeutil.add_minutes(gap_from, timeutil.diff_minutes(gap_to, gap_from) // 2)
            affected = self._bouquets_actually_measured_by(logger_id, midpoint)
            if not affected:
                continue
            orders = self._orders_for_bouquets(affected, statuses=("accepted", "partial_received", "received"))
            for order_id, bouquet_ids in orders.items():
                hit = [b for b in bouquet_ids if b in affected]
                window = {"from": gap_from, "to": gap_to}
                detail = (f"记录器 {logger_id} 在 {gap_from} ~ {gap_to} 断连，"
                          f"空档按保守温度计费，需承运方补传读数或说明")
                flag_id = self._merge_flag(order_id, "logger_disconnect", hit,
                                           self._shipment_for(hit), window, detail, at)
                if flag_id:
                    self._reevaluate_flag(flag_id, at)

    def _react_to_arrival(self, shipment_id, at):
        shipment = self.state.shipments[shipment_id]
        arrived_at = shipment["arrived_at"]
        departed_at = shipment["departed_at"]
        orders = self._orders_for_bouquets(
            self._bouquets_in_shipment(shipment_id),
            statuses=("accepted", "partial_received", "received"),
        )
        for order_id, bouquet_ids in orders.items():
            order = self.state.orders[order_id]
            window = order["arrival_window"]
            if arrived_at > window["until"]:
                detail = (f"车次 {shipment_id} 于 {arrived_at} 到达，"
                          f"晚于承诺窗口终点 {window['until']}")
                self._merge_flag(order_id, "late_delivery", bouquet_ids, shipment_id,
                                 {"from": window["until"], "to": arrived_at}, detail, at)
            # 在途中无有效温度读数覆盖的区间，按断连圈定等待承运方补证
            rule = order["rule_snapshot"]
            uncovered_bouquets = []
            worst = 0
            for bid in bouquet_ids:
                bouquet = self.state.bouquets[bid]
                uncovered = self._uncovered_transit_minutes(
                    rule, bouquet, departed_at, arrived_at
                )
                worst = max(worst, uncovered)
                if uncovered >= GAP:
                    uncovered_bouquets.append(bid)
            if uncovered_bouquets:
                detail = (f"车次 {shipment_id} 在途 {departed_at} ~ {arrived_at} "
                          f"存在无温度记录覆盖区间（最长 {worst} 分钟），"
                          f"已按保守温度计费，等待承运方补传读数或情况说明")
                self._merge_flag(order_id, "logger_disconnect", uncovered_bouquets, shipment_id,
                                 {"from": departed_at, "to": arrived_at}, detail, at)

    def _bouquets_in_shipment(self, shipment_id):
        result = []
        shipment = self.state.shipments[shipment_id]
        for box_id in shipment["box_ids"]:
            box = self.state.boxes.get(box_id)
            if box:
                result.extend(box["bouquet_ids"])
        return result

    def _react_to_receipt(self, order_id, at):
        order = self.state.orders[order_id]
        # 延误：实际签收晚于承诺窗口（逐束，分批签收时只圈当批）
        latest = order["receipts"][-1]
        window_until = order["arrival_window"]["until"]
        late = [b for b in latest["bouquet_ids"] if latest["received_at"] > window_until]
        if late:
            detail = f"{latest['received_at']} 签收，晚于承诺窗口终点 {window_until}"
            self._merge_flag(order_id, "late_delivery", late, latest.get("shipment_id"),
                             {"from": window_until, "to": latest["received_at"]}, detail, at)
        # 到货品相异常（收货方记录）
        damaged = [b for b in latest["bouquet_ids"] if latest["condition"] in ("damaged", "wilted")]
        if damaged:
            detail = f"收货方记录品相异常：{latest['condition']}（{latest.get('note', '')}）"
            self._merge_flag(order_id, "arrival_quality", damaged, latest.get("shipment_id"),
                             None, detail, at)
        # 每次签收后重评该单全部开放标志（补传/补证可能改变结论）
        for flag in list(self.state.flags.values()):
            if flag["order_id"] == order_id and flag["status"] not in ("ruled", "cleared"):
                self._reevaluate_flag(flag["id"], at)

    # ── 标志合并与自动重评 ─────────────────────────────────────
    def _open_flag(self, order_id, reason, bouquet_ids, shipment_id, window, detail, at, source="system"):
        flag_id = _new_id("flag")
        self._append(
            P.BOUQUETS_FLAGGED,
            {"flag_id": flag_id, "order_id": order_id, "shipment_id": shipment_id,
             "reason": reason, "bouquet_ids": sorted(set(bouquet_ids)), "window": window,
             "detail": detail, "source": source},
            at,
        )
        return flag_id

    def _merge_flag(self, order_id, reason, bouquet_ids, shipment_id, window, detail, at):
        """同一异常只圈一次：命中既有开放标志则并入花束/延展窗口，否则新开。"""
        existing = self._find_open_flag(order_id, reason, shipment_id, window)
        if existing:
            merged = sorted(set(existing["bouquet_ids"]) | set(bouquet_ids))
            new_window = existing.get("window")
            if window and new_window:
                new_window = {"from": min(new_window["from"], window["from"]),
                              "to": max(new_window["to"], window["to"])}
            elif window:
                new_window = dict(window)
            self._append(
                P.FLAG_UPDATED,
                {"flag_id": existing["id"], "bouquet_ids": merged,
                 "window": new_window, "detail": detail},
                at,
            )
            return existing["id"]
        return self._open_flag(order_id, reason, sorted(set(bouquet_ids)), shipment_id,
                               window, detail, at)

    def _find_open_flag(self, order_id, reason, shipment_id, window):
        for flag in self.state.flags.values():
            if flag["order_id"] != order_id or flag["reason"] != reason:
                continue
            if flag["status"] in ("ruled", "cleared"):
                continue
            # 同一车次的同类异常合并为一个标志（窗口自动延展），
            # 这样连续超温读数、到达与签收两次判延误都不会重复圈定。
            if shipment_id and flag.get("shipment_id") == shipment_id:
                return flag
            if shipment_id and flag.get("shipment_id") and flag["shipment_id"] != shipment_id:
                continue
            if window is None or flag.get("window") is None:
                return flag
            if flag["window"]["from"] == window["from"]:
                return flag
        return None

    def _reevaluate_flag(self, flag_id, at):
        """补传/补证后的自动重评。

        口径：
        - 超温/断连：重算受影响花束在异常窗口内的暴露。若窗口内
          已被读数完整覆盖且不再出现超温档（补传还原了真相），
          自动解除；否则维持开放，等待责任方补证。
        - 任何一方提交"免责"类证据时不自动解除，转由人工终裁，
          但证据会完整留在回放里。
        """
        flag = self.state.flags[flag_id]
        if flag["status"] in ("ruled", "cleared"):
            return
        order = self.state.orders[flag["order_id"]]
        rule = order["rule_snapshot"]
        if flag["reason"] in ("temperature_excursion", "logger_disconnect"):
            window = flag.get("window")
            if not window:
                return
            still_bad = set()
            for bid in flag["bouquet_ids"]:
                bouquet = self.state.bouquets[bid]
                bad_minutes = self._window_bad_minutes(
                    rule, bouquet, flag["reason"], window["from"], window["to"]
                )
                if bad_minutes > 0:
                    still_bad.add(bid)
            if not still_bad:
                self._append(
                    P.FLAG_CLEARED,
                    {"flag_id": flag_id,
                     "reason": "补传读数覆盖异常窗口，重算后无超温暴露"},
                    at,
                )
                return
            if set(flag["bouquet_ids"]) != still_bad:
                self._append(P.FLAG_UPDATED,
                             {"flag_id": flag_id, "bouquet_ids": sorted(still_bad),
                              "detail": flag["detail"]}, at)
        # 延误类：事实不因补证改变，保持开放等待责任方补证/终裁

    def _window_bad_minutes(self, rule, bouquet, reason, win_from, win_to):
        """窗口内仍未被洗白的异常分钟数。

        - 有真实读数且处于超温档：超温事实成立；
        - 无读数覆盖（断连）：仅对断连类标志计为异常；超温标志本身
          由真实读数坐实，空档不扩大其范围。
        补传后空档消失、读数回到冷藏档，结果归零，标志自动解除。
        """
        start = max(win_from, coldchain.accounting_start(bouquet))
        # 窗口为闭区间：from == to 时也包含那一分钟
        total = timeutil.minutes_between(start, win_to) + 1
        bad = 0
        for offset in range(total):
            minute = timeutil.add_minutes(start, offset)
            temp_c, _logger, _transit = coldchain._temp_at_minute(self.state, bouquet, minute, rule)
            if temp_c is not None:
                if band_for_temp(rule, temp_c).get("excursion"):
                    bad += 1
            elif reason == "logger_disconnect":
                bad += 1
        return bad

    def _uncovered_transit_minutes(self, rule, bouquet, departed_at, arrived_at):
        """在途区间内无有效温度读数覆盖的分钟数。"""
        start = max(departed_at, coldchain.accounting_start(bouquet))
        total = timeutil.minutes_between(start, arrived_at)
        uncovered = 0
        for offset in range(total):
            minute = timeutil.add_minutes(start, offset)
            if not coldchain._is_in_transit(bouquet, minute):
                continue
            temp_c, _logger, _transit = coldchain._temp_at_minute(self.state, bouquet, minute, rule)
            if temp_c is None:
                uncovered += 1
        return uncovered

    def _reevaluate_flags_for_reading(self, logger_id, reading_at, at):
        """补传读数落在某开放异常窗口内时，重评该标志能否解除。"""
        affected = self._bouquets_actually_measured_by(logger_id, reading_at)
        if not affected:
            return
        for flag in list(self.state.flags.values()):
            if flag["status"] in ("ruled", "cleared"):
                continue
            if flag["reason"] not in ("temperature_excursion", "logger_disconnect"):
                continue
            window = flag.get("window")
            if not window or not (window["from"] <= reading_at <= window["to"]):
                continue
            if affected.isdisjoint(set(flag["bouquet_ids"])):
                continue
            self._reevaluate_flag(flag["id"], at)

    # ── 查询辅助 ─────────────────────────────────────────────
    def _orders_for_bouquets(self, bouquet_ids, statuses=None):
        result = defaultdict(list)
        for order in self.state.orders.values():
            if statuses and order["status"] not in statuses:
                continue
            for bid in bouquet_ids:
                if bid in order["bouquet_ids"]:
                    result[order["id"]].append(bid)
        return dict(result)

    def _shipment_for(self, bouquet_ids):
        for bid in bouquet_ids:
            shipment_id = self.state.bouquets[bid].get("current_shipment")
            if shipment_id:
                return shipment_id
        return None

    def get_order(self, order_id):
        order = self.state.order(order_id)
        return deepcopy(order)

    def get_bouquet(self, bouquet_id):
        bouquet = self.state.bouquet(bouquet_id)
        return deepcopy(bouquet)

    def list_flags(self, order_id=None, status=None):
        flags = self.state.flags.values()
        if order_id:
            flags = [f for f in flags if f["order_id"] == order_id]
        if status:
            flags = [f for f in flags if f["status"] == status]
        return [deepcopy(f) for f in sorted(flags, key=lambda f: f["opened_at"])]

    # ── 9. 结算：一次结算，赔付/扣款/罚金各归其责 ─────────────
    def settle_order(self, order_id, at=None):
        """对订单做唯一一次结算。

        - 所有异常必须已闭环（解除或终裁），否则拒绝并列出待补证项；
        - 每束花给出承诺瓶插期 vs 按订单固化规则重算的实际瓶插期；
        - 缩水赔付按规则档位计算；运输异常赔客户、向承运方追偿；
          产地问题算种植户品质扣款；到货品相问题按责任归属。
        """
        at = self._at(at)
        order = self.state.order(order_id)
        if order["status"] == "settled":
            raise SettlementClosed("订单已结算，只能查询既有结算结果", order_id=order_id)
        if order_id in self.state.settlements:
            raise SettlementClosed("订单已结算", order_id=order_id)
        open_flags = [f for f in self.state.flags.values()
                      if f["order_id"] == order_id and f["status"] not in ("ruled", "cleared")]
        if open_flags:
            raise Conflict("仍有异常等待责任方补证，不能结算",
                           order_id=order_id,
                           open_flags=[{"flag_id": f["id"], "reason": f["reason"],
                                        "status": f["status"]} for f in open_flags])
        if order["status"] != "received":
            raise Conflict("订单尚未全部签收，不能结算",
                           order_id=order_id, status=order["status"])

        rule = order["rule_snapshot"]
        received_at = order["received_at"]
        amount_per = order["amount"] / len(order["bouquet_ids"])
        lines = []
        payout_total = 0.0
        grower_deduction_total = 0.0
        carrier_penalty_total = 0.0
        ruled = {f["id"]: f for f in self.state.flags.values()
                 if f["order_id"] == order_id and f["status"] == "ruled"}

        for bid in order["bouquet_ids"]:
            bouquet = self.state.bouquets[bid]
            commitment = order["commitments"][bid]
            promised_hours = commitment["promised_vase_hours"]
            cool = coldchain.cool_state(self.state, rule, bouquet, received_at)
            actual_hours = cool["remaining_vase_hours"]
            shortfall_ratio = max(0.0, (promised_hours - actual_hours) / promised_hours) if promised_hours else 0.0
            vase_pay_ratio = compensation_ratio(rule, shortfall_ratio)

            # 责任归集：该花束命中的标志及裁定结果
            reasons = []
            ruled_flags = []
            for flag in self.state.flags.values():
                if flag["order_id"] != order_id or bid not in flag["bouquet_ids"]:
                    continue
                if flag["status"] == "cleared":
                    reasons.append({"reason": flag["reason"], "outcome": "cleared"})
                    continue
                if flag["status"] == "ruled":
                    entry = {"reason": flag["reason"], "outcome": "ruled",
                             "liable_party": flag["liable_party"],
                             "resolution": flag.get("resolution")}
                    reasons.append(entry)
                    ruled_flags.append((flag, entry))

            # 到货品相异常按收货记录取定额档（需已裁定才生效）
            quality_pay_ratio = 0.0
            quality_condition = None
            for flag, _entry in ruled_flags:
                if flag["reason"] == "arrival_quality":
                    quality_condition = bouquet.get("receipt_condition")
                    quality_pay_ratio = max(
                        quality_pay_ratio,
                        rule.get("quality_compensation", {}).get(quality_condition, 0.0),
                    )

            # 同一束花的瓶插缩水与品相赔付只取较高档，不重复计赔
            pay_ratio = max(vase_pay_ratio, quality_pay_ratio)
            payout = round(amount_per * pay_ratio, 2)

            line = {
                "bouquet_id": bid,
                "amount": round(amount_per, 2),
                "promised_vase_hours": promised_hours,
                "actual_vase_hours": actual_hours,
                "shortfall_ratio": round(shortfall_ratio, 3),
                "receipt_condition": bouquet.get("receipt_condition"),
                "vase_compensation_ratio": vase_pay_ratio,
                "quality_compensation_ratio": quality_pay_ratio,
                "compensation_ratio": pay_ratio,
                "customer_payout": payout,
                "grower_deduction": 0.0,
                "carrier_penalty": 0.0,
                "reasons": reasons,
                "cool_detail": {
                    "budget_min": cool["budget_min"],
                    "consumed_min": cool["consumed_min"],
                    "remaining_min": cool["remaining_min"],
                    "disconnected_transit_min": cool["exposure"]["disconnected_transit_min"],
                    "connectivity_ratio": cool["exposure"]["connectivity_ratio"],
                },
            }

            carrier_fault = any(
                entry["liable_party"] == "carrier"
                for _flag, entry in ruled_flags
                if _flag["reason"] in ("temperature_excursion", "logger_disconnect",
                                       "late_delivery", "arrival_quality")
            )
            grower_quality_fault = any(
                flag["reason"] == "arrival_quality" and entry["liable_party"] == "grower"
                for flag, entry in ruled_flags
            )
            baseline = coldchain.baseline_vase_minutes(rule, bouquet) / 60
            standard = rule["vase_life_hours"][bouquet["cultivar"]]
            origin_shortfall = max(0.0, (standard - baseline) / standard)
            if grower_quality_fault and payout > 0:
                line["grower_deduction"] = payout
            elif payout > 0 and not carrier_fault and origin_shortfall >= 0.10:
                # 无承运责任时，基线（成熟度/预冷等待）低于品种标准 10% 以上计产地扣款
                line["grower_deduction"] = payout
            if carrier_fault and payout > 0:
                line["carrier_penalty"] = payout
            payout_total += line["customer_payout"]
            grower_deduction_total += line["grower_deduction"]
            carrier_penalty_total += line["carrier_penalty"]
            lines.append(line)

        settlement = {
            "order_id": order_id,
            "settled_at": at,
            "rule_version": rule["version"],
            "order_amount": order["amount"],
            "lines": lines,
            "customer_payout_total": round(payout_total, 2),
            "grower_deduction_total": round(grower_deduction_total, 2),
            "carrier_penalty_total": round(carrier_penalty_total, 2),
            "open_flags_cleared": [f["id"] for f in self.state.flags.values()
                                   if f["order_id"] == order_id and f["status"] == "cleared"],
        }
        self._append(P.SETTLEMENT_CREATED, settlement, at, actor="system")
        return deepcopy(settlement)

    def get_settlement(self, order_id):
        if order_id not in self.state.settlements:
            raise NotFound("订单尚未结算", order_id=order_id)
        return deepcopy(self.state.settlements[order_id])

    # ── 10. 回放：从采切到索赔的完整决定过程 ──────────────────
    def replay(self, order_id):
        order = self.state.order(order_id)
        bouquet_ids = set(order["bouquet_ids"])
        # 与这些花束相关的全部事件（含它们的批次、棚、班次、处理、箱、车、温度）
        relevant_boxes = set()
        relevant_shipments = set()
        relevant_loggers = set()
        for bid in bouquet_ids:
            bouquet = self.state.bouquets[bid]
            if bouquet.get("current_box"):
                relevant_boxes.add(bouquet["current_box"])
            if bouquet.get("current_shipment"):
                relevant_shipments.add(bouquet["current_shipment"])
        # 历史上经过的箱/车
        for bid in bouquet_ids:
            for entry in self.state.bouquets[bid]["chain"]:
                if entry.get("box_id"):
                    relevant_boxes.add(entry["box_id"])
                if entry.get("from_box_id"):
                    relevant_boxes.add(entry["from_box_id"])
                if entry.get("shipment_id"):
                    relevant_shipments.add(entry["shipment_id"])
        for shipment_id in relevant_shipments:
            shipment = self.state.shipments.get(shipment_id)
            if shipment:
                relevant_boxes.update(shipment["box_ids"])
                for leg in shipment["legs"]:
                    if leg.get("logger_id"):
                        relevant_loggers.add(leg["logger_id"])
        for box_id in relevant_boxes:
            # 箱记录器（取该箱历史全部绑定）
            for logger_id, logger in self.state.loggers.items():
                for binding in logger["bindings"]:
                    if binding["target_type"] == "box" and binding["target_id"] == box_id:
                        relevant_loggers.add(logger_id)

        timeline = []
        for event in self.store.all():
            if self._event_relates(event, order, bouquet_ids, relevant_boxes,
                                   relevant_shipments, relevant_loggers):
                timeline.append({
                    "seq": event["seq"], "at": event["at"], "type": event["type"],
                    "actor": event.get("actor"), "payload": event["payload"],
                })
        return {
            "order_id": order_id,
            "rule_version_at_acceptance": order["rule_version"],
            "timeline": timeline,
            "flags": self.list_flags(order_id),
            "settlement": self.state.settlements.get(order_id),
        }

    def _event_relates(self, event, order, bouquet_ids, boxes, shipments, loggers):
        etype = event["type"]
        p = event["payload"]
        if etype in (P.ORDER_ACCEPTED, P.SETTLEMENT_CREATED, P.RECEIPT_CONFIRMED):
            return p.get("order_id") == order["id"]
        if etype in (P.BOUQUETS_FLAGGED, P.FLAG_UPDATED, P.FLAG_CLEARED,
                     P.EVIDENCE_SUBMITTED, P.FLAG_RULED):
            return p.get("order_id") == order["id"] or (
                self.state.flags.get(p.get("flag_id"), {}).get("order_id") == order["id"]
            )
        if etype in (P.BOUQUET_HARVESTED, P.TREATMENT_RECORDED):
            return bool(bouquet_ids & set(p.get("bouquet_ids", []))) or p.get("bouquet_id") in bouquet_ids
        if etype == P.CUT_SHIFT_PERFORMED:
            return p["shift_id"] in {self.state.bouquets[b]["shift_id"] for b in bouquet_ids}
        if etype == P.GREENHOUSE_REGISTERED:
            return p["greenhouse_id"] in {self.state.bouquets[b]["greenhouse_id"] for b in bouquet_ids}
        if etype == P.SEED_BATCH_REGISTERED:
            return p["seed_batch_id"] in {self.state.bouquets[b]["seed_batch_id"] for b in bouquet_ids}
        if etype in (P.BOX_PACKED, P.BOX_SPLIT):
            box_id = p.get("box_id") or p.get("from_box_id")
            return box_id in boxes
        if etype in (P.SHIPMENT_CREATED, P.VEHICLE_CHANGED, P.SHIPMENT_ARRIVED, P.CARRIER_CALLBACK):
            return p.get("shipment_id") in shipments
        if etype == P.TEMP_READING:
            return p["logger_id"] in loggers
        if etype == P.LOGGER_BOUND:
            return p["logger_id"] in loggers
        return False
