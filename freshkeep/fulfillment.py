"""履约监测、异常圈定、补证、理赔与结算。

关键规则：
- 承运方回调按 event_id 幂等，重复回调只返回首次事实；
- 收货方可分批签收（receipt_id 幂等），每束花只签收一次，一行只结算一次；
- 超温/断连/延误在扫描时按订单冻结的规则版本判定，自动圈出受影响花束，
  事故进入“等待责任方补证”，补证 + 裁定后才可结算；
- 复盘温度按实测时间线重算每束花剩余瓶插天数；断连段是否按异常温度计，
  以对该缺口事故的裁定结论为准；
- 赔付比例全部取自订单快照中的规则，新旧订单互不串改。
"""

from collections import defaultdict

from .clock import now_iso, parse_iso, to_iso
from . import coldlife


class FulfillmentError(ValueError):
    pass


# 事故状态
OPEN = "open"
AWAITING_EVIDENCE = "awaiting_evidence"
ADJUDICATED = "adjudicated"
# 责任方
LIABILITY_CARRIER = "carrier"
LIABILITY_GROWER = "grower"
LIABILITY_PLATFORM = "platform"
LIABILITY_NONE = "none"


class FulfillmentService:
    def __init__(self, store, lineage, monitoring, ordering, clock=None):
        self.store = store
        self.lineage = lineage
        self.monitoring = monitoring
        self.ordering = ordering
        self.clock = clock

    def _at(self, at=None):
        return parse_iso(at) if at else (parse_iso(self.clock.now_iso()) if self.clock else parse_iso(now_iso()))

    # ---- 承运方回调（幂等） ---------------------------------------------

    def carrier_event(self, event_id, order_id, event_type, at, vehicle_id=None,
                      line_id=None, payload=None):
        """承运方事件回调。event_type: departed|arrived|transferred|delayed。"""
        at_dt = self._at(at)
        with self.store.lock():
            if order_id not in self.store.orders:
                raise FulfillmentError(f"订单不存在: {order_id}")
            if event_id in self.store.delivery_events:
                existing = self.store.delivery_events[event_id]
                return {"event": existing, "deduplicated": True}
            event = {
                "event_id": event_id, "order_id": order_id, "line_id": line_id,
                "type": event_type, "vehicle_id": vehicle_id,
                "at": to_iso(at_dt), "payload": payload or {},
                "received_at": to_iso(self._at()),
            }
            self.store.delivery_events[event_id] = event
            self.store.add_decision(
                "carrier_event", order_id,
                f"承运回调 {event_type}（车辆 {vehicle_id}，{event['at']}）",
                inputs={"event_id": event_id, "vehicle_id": vehicle_id},
                outputs={"type": event_type, "at": event["at"]},
                at=event["received_at"], actor="carrier",
            )
            return {"event": event, "deduplicated": False}

    # ---- 分批签收（幂等，只结算一次） ------------------------------------

    def sign_receipt(self, receipt_id, order_id, line_id, bouquet_ids, at,
                     condition_note=None, receiver=None):
        at_dt = self._at(at)
        with self.store.lock():
            if order_id not in self.store.orders:
                raise FulfillmentError(f"订单不存在: {order_id}")
            line = self.ordering._line(order_id, line_id)
            if receipt_id in self.store.receipts:
                return {"receipt": self.store.receipts[receipt_id], "deduplicated": True}
            bids = list(bouquet_ids)
            unknown = [b for b in bids if b not in line["bouquet_ids"]]
            if unknown:
                raise FulfillmentError(f"花束不属于该订单行: {unknown}")
            already = [b for b in bids if b in line["received_bouquet_ids"]]
            if already:
                raise FulfillmentError(f"花束已签收，不能重复签收: {already}")
            receipt = {
                "receipt_id": receipt_id, "order_id": order_id, "line_id": line_id,
                "bouquet_ids": bids, "at": to_iso(at_dt),
                "condition_note": condition_note, "receiver": receiver,
            }
            self.store.receipts[receipt_id] = receipt
            line["receipts"].append(receipt)
            line["received_bouquet_ids"].extend(bids)
            self.store.add_decision(
                "receipt_signed", order_id,
                f"分批签收 +{len(bids)} 束（累计 {len(line['received_bouquet_ids'])}"
                f"/{len(line['bouquet_ids'])}）",
                inputs={"receipt_id": receipt_id, "line_id": line_id, "bouquet_ids": bids},
                outputs={"received_total": len(line["received_bouquet_ids"]),
                         "fully_received": set(line["received_bouquet_ids"]) == set(line["bouquet_ids"])},
                at=receipt["at"], actor="receiver",
            )
            fully = set(line["received_bouquet_ids"]) == set(line["bouquet_ids"])
        # 锁外扫描（扫描内部自取锁）
        self.scan_order(order_id)
        if fully:
            self._ensure_claim(order_id, line_id)
        return {"receipt": receipt, "deduplicated": False, "fully_received": fully}

    # ---- 异常扫描（确定性、可重入） --------------------------------------

    def scan_order(self, order_id):
        """按订单冻结规则扫描全部行的超温、断连、延误事故。重复执行不产生重复事故。"""
        with self.store.lock():
            order = self.store.orders.get(order_id)
            if not order:
                raise FulfillmentError(f"订单不存在: {order_id}")
            results = []
            for line in order["lines"].values():
                if line["promise"]["status"] != "promised":
                    continue
                results += self._scan_temperature(order, line)
                results += self._scan_delay(order, line)
            # 维护订单状态：被拒行不参与交付完成判定
            promised_lines = [l for l in order["lines"].values()
                              if l["promise"]["status"] == "promised"]
            if promised_lines and all(
                set(l["received_bouquet_ids"]) == set(l["bouquet_ids"])
                for l in promised_lines
            ):
                order["status"] = "delivered"
            self._refresh_claims(order_id)
            return [self.public_incident(i["incident_id"]) for i in results]

    def scan_all_orders(self):
        """补传等事实更新后，重扫所有在途订单。"""
        out = []
        for order_id in list(self.store.orders.keys()):
            out += self.scan_order(order_id)
        return out

    def _params(self, line):
        # 同一行各束的渠道参数一致；取任一冻结参数即可（阈值无品种差异时）
        first = line["bouquet_ids"][0]
        return line["resolved_params"][first]

    def _scan_temperature(self, order, line):
        params = self._params(line)
        exc = params["excursion"]
        line_set = set(line["bouquet_ids"])
        found = []
        win_start = order["placed_at"]
        win_end_dt = self._at()
        if line["receipts"]:
            win_end_dt = max(parse_iso(r["at"]) for r in line["receipts"])
        win_end = to_iso(win_end_dt)

        # 以记录仪绑定为准（即使断连期间没有任何读数，也要能扫出缺口事故）
        with self.store.lock():
            bindings = dict(self.store.logger_bindings)
        for logger_id, binding in bindings.items():
            box_id = binding["container_id"]
            # 该记录仪绑定的箱在时间窗内是否承载过本行花束
            touched = bool(line_set & set(
                self.lineage.bouquets_in_container_during(box_id, win_start, win_end)))
            if not touched:
                continue
            items = []
            for bid in line["bouquet_ids"]:
                for r in self.monitoring.readings_for_bouquet(bid, win_start, win_end):
                    if r["logger_id"] == logger_id:
                        items.append((bid, r))
            readings = sorted({r["seq"]: r for _, r in items}.values(),
                              key=lambda r: (parse_iso(r["read_at"]), r["seq"]))
            # 连续超温段：相邻读数均高于阈值视为一段
            run = None
            for r in readings + [None]:
                hot = r is not None and r["temp_c"] >= exc["minor_above_c"]
                if hot:
                    if run is None:
                        run = {"start": r, "end": r, "major": r["temp_c"] >= exc["major_above_c"]}
                    else:
                        run["end"] = r
                        run["major"] = run["major"] or r["temp_c"] >= exc["major_above_c"]
                elif run is not None:
                    self._emit_excursion(order, line, logger_id, box_id, run,
                                         exc, line_set, found)
                    run = None

            # 记录仪断连缺口
            for gap in self.monitoring.gaps(logger_id, win_start, win_end):
                g_start = parse_iso(gap["disconnected_at"])
                g_end = parse_iso(gap["resumed_at"]) if gap.get("resumed_at") else win_end_dt
                if (g_end - g_start).total_seconds() / 60.0 < exc["gap_tolerance_minutes"]:
                    continue
                self._emit_gap_incident(order, line, logger_id, box_id, gap,
                                        g_start, g_end, line_set, found)
        return found

    def _emit_excursion(self, order, line, logger_id, box_id, run, exc_cfg,
                        line_set, found):
        start_dt, end_dt = parse_iso(run["start"]["read_at"]), parse_iso(run["end"]["read_at"])
        minutes = (end_dt - start_dt).total_seconds() / 60.0
        if minutes < exc_cfg["minor_minutes"]:
            return  # 瞬时波动不构成事故
        severity = "major" if run["major"] else "minor"
        peak = max(run["start"]["temp_c"], run["end"]["temp_c"])
        vehicle_id = self.lineage.vehicle_of_box_at(box_id, start_dt + (end_dt - start_dt) / 2)
        affected = sorted(line_set & set(
            self.lineage.bouquets_in_container_during(box_id, start_dt, end_dt)))
        if not affected:
            return
        incident_id = f"TEMP:{order['order_id']}:{line['line_id']}:{logger_id}:{run['start']['seq']}"
        incident = self._upsert_incident(
            incident_id, order["order_id"], "temperature_excursion", severity,
            to_iso(start_dt), to_iso(end_dt),
            {"logger_id": logger_id, "box_id": box_id, "vehicle_id": vehicle_id,
             "peak_temp_c": peak, "duration_minutes": round(minutes, 1),
             "threshold_minor_c": exc_cfg["minor_above_c"],
             "threshold_major_c": exc_cfg["major_above_c"]},
            line["line_id"], affected,
        )
        if incident:
            found.append(incident)

    def _emit_gap_incident(self, order, line, logger_id, box_id, gap,
                           start_dt, end_dt, line_set, found):
        affected = sorted(line_set & set(
            self.lineage.bouquets_in_container_during(box_id, start_dt, end_dt)))
        if not affected:
            return
        incident_id = f"GAP:{order['order_id']}:{line['line_id']}:{gap['gap_id']}"
        existing = self.store.incidents.get(incident_id)
        incident = self._upsert_incident(
            incident_id, order["order_id"], "temperature_gap", "minor",
            to_iso(start_dt), to_iso(end_dt),
            {"logger_id": logger_id, "box_id": box_id, "gap_id": gap["gap_id"],
             "gap_status": gap["status"], "reason": gap.get("reason"),
             "duration_minutes": round((end_dt - start_dt).total_seconds() / 60.0, 1)},
            line["line_id"], affected,
        )
        # 已补传读数：缺口事实闭合，温度责任按实测时间线判定，不再等待补证
        if gap["status"] == "backfilled":
            target = existing or incident
            if target and target["status"] != ADJUDICATED:
                self._adjudicate_locked(
                    target, LIABILITY_NONE,
                    "记录仪断连后已补传数据，缺口事实闭合，温度责任按实测读数判定",
                    {"temp_treatment": "measured"}, actor="system",
                )
        if incident:
            found.append(incident)

    def _scan_delay(self, order, line):
        found = []
        if not line["receipts"]:
            return found
        committed_at = line["promise"].get("committed_arrival_by")
        if not committed_at:
            return found
        last_dt = max(parse_iso(r["at"]) for r in line["receipts"])
        late_minutes = (last_dt - parse_iso(committed_at)).total_seconds() / 60.0
        if late_minutes <= 0:
            return found
        params = self._params(line)
        major_gate = params["compensation"]["delay_bands"][0]["late_minutes_gte"]
        incident_id = f"DELAY:{order['order_id']}:{line['line_id']}"
        existing = self.store.incidents.get(incident_id)
        incident = self._upsert_incident(
            incident_id, order["order_id"], "delivery_delay",
            "major" if late_minutes >= major_gate else "minor",
            committed_at, to_iso(last_dt),
            {"late_minutes": round(late_minutes, 1),
             "committed_arrival_by": committed_at},
            line["line_id"], list(line["bouquet_ids"]),
        )
        if incident and not existing:
            found.append(incident)
        return found

    def _upsert_incident(self, incident_id, order_id, itype, severity, start_at,
                         end_at, facts, line_id, affected_bouquets):
        """新建事故；已存在则补齐花束圈定，不覆盖补证/裁定状态。返回事故（新建时）。"""
        created = False
        incident = self.store.incidents.get(incident_id)
        if incident is None:
            incident = {
                "incident_id": incident_id,
                "order_id": order_id,
                "type": itype,
                "severity": severity,
                "status": AWAITING_EVIDENCE,
                "start_at": start_at,
                "end_at": end_at,
                "facts": facts,
                "lines": {line_id: sorted(affected_bouquets)},
                "evidence": [],
                "liability": None,
                "liability_note": None,
                "outcome": {},
                "created_at": to_iso(self._at()),
                "adjudicated_at": None,
            }
            self.store.incidents[incident_id] = incident
            created = True
        else:
            incident["lines"].setdefault(line_id, [])
            for b in affected_bouquets:
                if b not in incident["lines"][line_id]:
                    incident["lines"][line_id].append(b)
            incident["facts"].update({k: v for k, v in facts.items() if k not in incident["facts"]})
        line = self.store.order_lines[order_id][line_id]
        if incident_id not in line["incident_ids"]:
            line["incident_ids"].append(incident_id)
        by_b = line["incidents_by_bouquet"]
        for b in affected_bouquets:
            by_b.setdefault(b, [])
            if incident_id not in by_b[b]:
                by_b[b].append(incident_id)
        if created:
            self.store.add_decision(
                "incident_detected", order_id,
                f"自动圈定事故 {itype}/{severity}，受影响 {len(affected_bouquets)} 束，"
                "等待责任方补证",
                inputs={"incident_id": incident_id, "facts": incident["facts"]},
                outputs={"affected_bouquets": {line_id: sorted(affected_bouquets)},
                         "status": AWAITING_EVIDENCE},
                rule_version=self.store.orders[order_id]["rule_version_id"],
                at=to_iso(self._at()), actor="system",
            )
            return incident
        return None

    # ---- 补证与裁定 ------------------------------------------------------

    def submit_evidence(self, incident_id, party, explanation, evidence_ref=None,
                        documents=None, at=None):
        with self.store.lock():
            incident = self._require_incident(incident_id)
            entry = {
                "party": party, "explanation": explanation,
                "evidence_ref": evidence_ref, "documents": documents or [],
                "at": to_iso(self._at(at)),
            }
            incident["evidence"].append(entry)
            if incident["status"] == OPEN:
                incident["status"] = AWAITING_EVIDENCE
            self.store.add_decision(
                "evidence_submitted", incident["order_id"],
                f"责任方 {party} 就事故 {incident_id} 提交补证",
                inputs={"incident_id": incident_id, "party": party,
                        "explanation": explanation, "evidence_ref": evidence_ref},
                outputs={"evidence_count": len(incident["evidence"])},
                at=entry["at"], actor=party,
            )
            return self.public_incident(incident_id)

    def adjudicate(self, incident_id, liability, note=None, outcome=None,
                   actor="manager", at=None):
        with self.store.lock():
            incident = self._require_incident(incident_id)
            result = self._adjudicate_locked(incident, liability, note, outcome, actor, at)
            self._refresh_claims(incident["order_id"])
            return result

    def _adjudicate_locked(self, incident, liability, note=None, outcome=None,
                           actor="manager", at=None):
        if liability not in (LIABILITY_CARRIER, LIABILITY_GROWER,
                             LIABILITY_PLATFORM, LIABILITY_NONE):
            raise FulfillmentError(f"未知责任方: {liability}")
        incident["status"] = ADJUDICATED
        incident["liability"] = liability
        incident["liability_note"] = note
        incident["outcome"] = outcome or {}
        incident["adjudicated_at"] = to_iso(self._at(at))
        self.store.add_decision(
            "incident_adjudicated", incident["order_id"],
            f"事故 {incident['incident_id']} 裁定：责任方={liability}",
            inputs={"incident_id": incident["incident_id"], "note": note,
                    "evidence_count": len(incident["evidence"])},
            outputs={"liability": liability, "outcome": incident["outcome"]},
            at=incident["adjudicated_at"], actor=actor,
        )
        return self.public_incident(incident["incident_id"])

    def _require_incident(self, incident_id):
        incident = self.store.incidents.get(incident_id)
        if not incident:
            raise FulfillmentError(f"事故不存在: {incident_id}")
        return incident

    def public_incident(self, incident_id):
        with self.store.lock():
            inc = self._require_incident(incident_id)
            return {
                "incident_id": inc["incident_id"],
                "order_id": inc["order_id"],
                "type": inc["type"],
                "severity": inc["severity"],
                "status": inc["status"],
                "start_at": inc["start_at"],
                "end_at": inc["end_at"],
                "facts": dict(inc["facts"]),
                "affected_bouquets": {k: list(v) for k, v in inc["lines"].items()},
                "evidence": list(inc["evidence"]),
                "liability": inc["liability"],
                "liability_note": inc["liability_note"],
                "outcome": dict(inc["outcome"]),
                "adjudicated_at": inc["adjudicated_at"],
            }

    def list_incidents(self, order_id=None):
        with self.store.lock():
            ids = [i for i in self.store.incidents
                   if order_id is None or self.store.incidents[i]["order_id"] == order_id]
            return [self.public_incident(i) for i in ids]

    # ---- 实测复盘：按花束重算剩余瓶插天数 -------------------------------

    def _bouquet_actual_segments(self, line, bid, arrival_dt):
        """把采切→到货拆成 (温度℃, 小时) 段。

        正常段温度取实测读数（前值延续）；已登记的断连缺口为独立段，
        温度按事故裁定处理（豁免=理想冷链，未豁免=环境温度从严）。
        """
        prov = line["frozen_provenance"][bid]
        params = line["resolved_params"][bid]
        cut_dt = parse_iso(prov["cut_at"])
        # 预冷时点已在接单时冻结，事后修改采后记录不影响旧订单复盘
        precool_dt = parse_iso(prov["precool_started_at"])
        timed = []
        if precool_dt > cut_dt:
            timed.append((cut_dt, precool_dt, params["ambient_temp_c"]))

        readings = self.monitoring.readings_for_bouquet(bid, precool_dt, arrival_dt)
        # 断连缺口（含“设备损坏、无读数可补”的情形）；
        # 已补传读数的缺口（measured）不单独成段，直接采用实测时间线
        gaps = []
        for inc_id in line["incidents_by_bouquet"].get(bid, []):
            inc = self.store.incidents[inc_id]
            if inc["type"] != "temperature_gap":
                continue
            if inc["status"] == ADJUDICATED and inc["liability"] == LIABILITY_NONE \
                    and inc["outcome"].get("temp_treatment") == "measured":
                continue
            g_start = max(parse_iso(inc["start_at"]), precool_dt)
            g_end = min(parse_iso(inc["end_at"]), arrival_dt)
            if g_end > g_start:
                gaps.append((g_start, g_end, self._gap_treatment_temp(params, inc)))
        gaps.sort()

        # 实测段：读数点之间前值延续，首点之前按理想冷链
        measured = []
        if readings:
            first_t = parse_iso(readings[0]["read_at"])
            if first_t > precool_dt:
                measured.append((precool_dt, first_t, params["ideal_chain_temp_c"]))
            for prev_r, cur_r in zip(readings, readings[1:]):
                measured.append((parse_iso(prev_r["read_at"]),
                                 parse_iso(cur_r["read_at"]), prev_r["temp_c"]))
            measured.append((parse_iso(readings[-1]["read_at"]),
                             arrival_dt, readings[-1]["temp_c"]))
        else:
            measured.append((precool_dt, arrival_dt, params["ideal_chain_temp_c"]))

        # 缺口段优先，从实测段中扣除
        for a, b, temp in measured:
            for piece_a, piece_b in self._subtract_gaps(a, b, gaps):
                timed.append((piece_a, piece_b, temp))
        timed.extend(gaps)
        timed.sort(key=lambda x: x[0])
        return [(temp, (b - a).total_seconds() / 3600.0)
                for a, b, temp in timed if b > a]

    @staticmethod
    def _subtract_gaps(a, b, gaps):
        """区间 [a,b] 扣除所有缺口区间后的剩余子区间。"""
        pieces = [(a, b)]
        for g_start, g_end, _temp in gaps:
            next_pieces = []
            for pa, pb in pieces:
                if g_end <= pa or g_start >= pb:
                    next_pieces.append((pa, pb))
                else:
                    if g_start > pa:
                        next_pieces.append((pa, min(pb, g_start)))
                    if g_end < pb:
                        next_pieces.append((max(pa, g_end), pb))
            pieces = next_pieces
        return pieces

    @staticmethod
    def _gap_treatment_temp(params, incident):
        """缺测段温度：事故被豁免按理想温度；未裁定/有责任按环境温度从严计算。"""
        if incident["status"] == ADJUDICATED and incident["liability"] == LIABILITY_NONE:
            treatment = incident["outcome"].get("temp_treatment")
            if treatment in ("ideal", "measured"):
                return params["ideal_chain_temp_c"]
            return params["ambient_temp_c"]
        return params["ambient_temp_c"]

    def reassess_bouquet(self, order_id, line_id, bid):
        with self.store.lock():
            order = self.store.orders[order_id]
            line = self.ordering._line(order_id, line_id)
            arrival_dt = (max(parse_iso(r["at"]) for r in line["receipts"])
                          if line["receipts"] else self._at())
            segments = self._bouquet_actual_segments(line, bid, arrival_dt)
            params = line["resolved_params"][bid]
            consumed = coldlife.consumed_hours(
                segments, params["q10"], params["reference_temp_c"])
            budget = coldlife.budget_hours(self._base_days(line, bid))
            actual_days = coldlife.remaining_vase_days(budget, consumed)
            return {
                "bouquet_id": bid,
                "arrival_at": to_iso(arrival_dt),
                "segments": [{"temp_c": t, "hours": round(h, 3)} for t, h in segments],
                "budget_hours": round(budget, 3),
                "consumed_hours": round(consumed, 3),
                "actual_vase_days_at_arrival": round(actual_days, 3),
                "promised_vase_days": line["promise"].get("promised_vase_days"),
            }

    @staticmethod
    def _base_days(line, bid):
        # 冻结来源不含基础天数，从接单时的逐束评估里取
        return line["promise"]["per_bouquet"][bid]["base_vase_days"]

    # ---- 理赔单 ----------------------------------------------------------

    def _ensure_claim(self, order_id, line_id):
        with self.store.lock():
            order = self.store.orders[order_id]
            line = self.ordering._line(order_id, line_id)
            claim_id = f"CLAIM:{order_id}:{line_id}"
            claim = self.store.claims.get(claim_id)
            if claim is None:
                claim = {
                    "claim_id": claim_id, "order_id": order_id, "line_id": line_id,
                    "status": "waiting_evidence",
                    "created_at": to_iso(self._at()),
                    "incident_ids": [],
                    "bouquet_reassessments": {},
                }
                self.store.claims[claim_id] = claim
                line["claim_ids"].append(claim_id)
                self.store.add_decision(
                    "claim_opened", order_id,
                    f"订单行 {line_id} 签收完成，进入理赔核验",
                    inputs={"line_id": line_id}, outputs={"claim_id": claim_id},
                    rule_version=order["rule_version_id"], at=claim["created_at"],
                    actor="system",
                )
            self._refresh_claim_locked(order, line, claim)
            return self.public_claim(claim_id)

    def _refresh_claims(self, order_id):
        order = self.store.orders.get(order_id)
        if not order:
            return
        for line in order["lines"].values():
            for claim_id in line["claim_ids"]:
                self._refresh_claim_locked(order, line, self.store.claims[claim_id])

    def _refresh_claim_locked(self, order, line, claim):
        incidents = [self.store.incidents[i] for i in line["incident_ids"]]
        claim["incident_ids"] = list(line["incident_ids"])
        pending = [i["incident_id"] for i in incidents if i["status"] != ADJUDICATED]
        if line.get("settlement_id"):
            claim["status"] = "settled"
        elif pending:
            claim["status"] = "waiting_evidence"
        else:
            claim["status"] = "payable"
        claim["pending_incident_ids"] = pending

    def public_claim(self, claim_id):
        with self.store.lock():
            claim = self.store.claims.get(claim_id)
            if not claim:
                raise FulfillmentError(f"理赔单不存在: {claim_id}")
            return {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
                    for k, v in claim.items()}

    def list_claims(self, order_id=None):
        with self.store.lock():
            ids = [c for c in self.store.claims
                   if order_id is None or self.store.claims[c]["order_id"] == order_id]
            return [self.public_claim(i) for i in ids]

    # ---- 结算：一行一次 --------------------------------------------------

    def settle_line(self, order_id, line_id, at=None):
        with self.store.lock():
            order = self.store.orders.get(order_id)
            if not order:
                raise FulfillmentError(f"订单不存在: {order_id}")
            line = self.ordering._line(order_id, line_id)
            if line["settlement_id"]:
                return {"settlement": self.public_settlement(line["settlement_id"]),
                        "deduplicated": True}
            if set(line["received_bouquet_ids"]) != set(line["bouquet_ids"]):
                raise FulfillmentError("尚未全部签收，不能结算")
            pending = [i for i in line["incident_ids"]
                       if self.store.incidents[i]["status"] != ADJUDICATED]
            if pending:
                raise FulfillmentError(
                    f"仍有事故等待责任方补证/裁定，不能结算: {pending}")

            arrival_dt = max(parse_iso(r["at"]) for r in line["receipts"])
            params = self._params(line)
            comp = params["compensation"]
            promised = line["promise"].get("promised_vase_days")
            price = float(line["unit_price"])
            n = len(line["bouquet_ids"])

            # 事故给每束花带来的赔付比例与责任归属
            bouquet_ratios = defaultdict(lambda: defaultdict(float))
            rationale = []
            for inc_id in line["incident_ids"]:
                inc = self.store.incidents[inc_id]
                if inc["liability"] == LIABILITY_NONE:
                    continue
                party = inc["liability"]
                affected = set(inc["lines"].get(line_id, []))
                ratio = self._incident_refund_ratio(inc, comp)
                for b in affected:
                    bouquet_ratios[b][party] = max(bouquet_ratios[b][party], ratio)
                rationale.append({
                    "incident_id": inc_id, "type": inc["type"],
                    "severity": inc["severity"], "liability": inc["liability"],
                    "refund_ratio": ratio, "affected_bouquets": sorted(affected),
                    "basis": self._comp_basis(inc),
                })

            # 实测瓶插复盘：短期缺口按 bands 归责（承运事故为主，否则种植/平台兜底）
            reassessments = {}
            for bid in line["bouquet_ids"]:
                r = self.reassess_bouquet(order_id, line_id, bid)
                reassessments[bid] = r
                actual = r["actual_vase_days_at_arrival"]
                if promised and actual < promised:
                    shortfall_ratio = (promised - actual) / promised
                    band_ratio = self._band_ratio(
                        comp["vase_shortfall_bands"], "shortfall_ratio_gte",
                        shortfall_ratio)
                    if band_ratio > 0:
                        party = self._attribute_shortfall(line, bid)
                        bouquet_ratios[bid][party] = max(
                            bouquet_ratios[bid][party], band_ratio)
                        rationale.append({
                            "incident_id": None, "type": "vase_shortfall",
                            "severity": "minor", "liability": party,
                            "refund_ratio": band_ratio,
                            "affected_bouquets": [bid],
                            "basis": {"promised": promised, "actual": actual,
                                      "shortfall_ratio": round(shortfall_ratio, 3)},
                        })

            # 种植端品质扣款：采切后预冷等待超时
            warning_hours = params["precool_wait_warning_hours"]
            rate = comp["grower_precool_deduction_per_hour"]
            cap = comp["grower_precool_deduction_cap"]
            per_bouquet_detail = []
            grower_precool_total = 0.0
            refund_by_party = defaultdict(float)
            for bid in line["bouquet_ids"]:
                wait_hours = line["frozen_provenance"][bid]["precool_wait_minutes"] / 60.0
                over = max(0.0, wait_hours - warning_hours)
                ded_ratio = min(cap, over * rate)
                grower_precool_total += price * ded_ratio
                ratios = bouquet_ratios.get(bid, {})
                refund_ratio = min(1.0, max(ratios.values(), default=0.0))
                for party, rr in ratios.items():
                    refund_by_party[party] += price * rr
                per_bouquet_detail.append({
                    "bouquet_id": bid,
                    "precool_wait_hours": round(wait_hours, 3),
                    "precool_deduction_ratio": round(ded_ratio, 4),
                    "refund_ratio": round(refund_ratio, 4),
                    "refund_by_party": {p: round(price * rr, 2) for p, rr in ratios.items()},
                    "actual_vase_days": reassessments[bid]["actual_vase_days_at_arrival"],
                    "promised_vase_days": promised,
                })

            gross = round(price * n, 2)
            grower_precool_total = round(grower_precool_total, 2)
            carrier_refund = round(refund_by_party.get(LIABILITY_CARRIER, 0.0), 2)
            grower_refund = round(refund_by_party.get(LIABILITY_GROWER, 0.0), 2)
            platform_refund = round(refund_by_party.get(LIABILITY_PLATFORM, 0.0), 2)
            customer_refund = round(carrier_refund + grower_refund + platform_refund, 2)
            grower_payout = round(gross - grower_precool_total - grower_refund, 2)
            settlement = {
                "settlement_id": f"SETTLE:{order_id}:{line_id}",
                "order_id": order_id, "line_id": line_id,
                "rule_version_id": order["rule_version_id"],
                "settled_at": to_iso(self._at(at)),
                "currency": "CNY",
                "gross_amount": gross,
                "customer_refund": customer_refund,
                "refund_by_party": {
                    LIABILITY_CARRIER: carrier_refund,
                    LIABILITY_GROWER: grower_refund,
                    LIABILITY_PLATFORM: platform_refund,
                },
                "grower_precool_deduction": grower_precool_total,
                "grower_payout": grower_payout,
                "per_bouquet": per_bouquet_detail,
                "rationale": rationale,
            }
            self.store.settlements[settlement["settlement_id"]] = settlement
            line["settlement_id"] = settlement["settlement_id"]
            claim_id = f"CLAIM:{order_id}:{line_id}"
            if claim_id in self.store.claims:
                self.store.claims[claim_id]["status"] = "settled"
            self.store.add_decision(
                "settled", order_id,
                f"订单行 {line_id} 一次性结算：退款 {customer_refund}，"
                f"种植扣款 {grower_precool_total}，种植户应得 {grower_payout}",
                inputs={"line_id": line_id, "rule_version_id": order["rule_version_id"],
                        "incident_ids": line["incident_ids"]},
                outputs={"gross": gross, "customer_refund": customer_refund,
                         "grower_payout": grower_payout,
                         "grower_precool_deduction": grower_precool_total},
                rule_version=order["rule_version_id"],
                at=settlement["settled_at"], actor="finance",
            )
            return {"settlement": settlement, "deduplicated": False}

    @staticmethod
    def _incident_refund_ratio(incident, comp):
        if incident["type"] == "temperature_excursion":
            return (comp["major_excursion_refund_ratio"] if incident["severity"] == "major"
                    else comp["minor_excursion_refund_ratio"])
        if incident["type"] == "delivery_delay":
            return FulfillmentService._band_ratio(
                comp["delay_bands"], "late_minutes_gte",
                incident["facts"]["late_minutes"])
        if incident["type"] == "temperature_gap":
            return comp["minor_excursion_refund_ratio"]
        return 0.0

    @staticmethod
    def _comp_basis(incident):
        basis = {"rule_thresholds": {
            k: v for k, v in incident["facts"].items()
            if k.startswith("threshold_") or k in ("late_minutes", "peak_temp_c",
                                                   "duration_minutes")}}
        return basis

    @staticmethod
    def _band_ratio(bands, gate_key, value):
        ratio = 0.0
        for band in bands:
            if value >= band[gate_key]:
                ratio = max(ratio, band["refund_ratio"])
        return ratio

    def _attribute_shortfall(self, line, bid):
        """瓶插不足的兜底归责：有承运事故归承运；仅预冷等待违规归种植；否则平台。"""
        for inc_id in line["incidents_by_bouquet"].get(bid, []):
            inc = self.store.incidents[inc_id]
            if inc["status"] == ADJUDICATED and inc["liability"] != LIABILITY_NONE:
                return inc["liability"]
        params = line["resolved_params"][bid]
        wait_hours = line["frozen_provenance"][bid]["precool_wait_minutes"] / 60.0
        if wait_hours > params["precool_wait_warning_hours"]:
            return LIABILITY_GROWER
        return LIABILITY_PLATFORM

    def public_settlement(self, settlement_id):
        with self.store.lock():
            s = self.store.settlements.get(settlement_id)
            if not s:
                raise FulfillmentError(f"结算记录不存在: {settlement_id}")
            return dict(s)
