"""接单与承诺服务。

接单瞬间做三件不可反悔的事：
1. 固化当时有效的品质规则全文（快照），逐品种解析参数；
2. 固化每束花的来源摘要（种苗/棚/班次/采后/成熟度/预冷等待）；
3. 用剩余冷量算出可承诺到货窗口与瓶插天数（一行多束时按最差束承诺）。

旧订单永远携带自己的快照，事后发布的宽松规则无法改变它。
"""

from datetime import timedelta

from .clock import now_iso, parse_iso, to_iso
from . import coldlife


class OrderError(ValueError):
    pass


class OrderingService:
    def __init__(self, store, lineage, rules, clock=None):
        self.store = store
        self.lineage = lineage
        self.rules = rules
        self.clock = clock

    def _now(self):
        return parse_iso(self.clock.now_iso()) if self.clock else parse_iso(now_iso())

    def place_order(self, order_id, lines, channel_default=None, placed_at=None,
                    customer=None, note=None):
        """接单。lines: [{line_id, bouquet_ids:[...], channel, unit_price,
        planned_arrival_at | planned_transit_hours}]。"""
        placed_dt = parse_iso(placed_at) if placed_at else self._now()
        with self.store.lock():
            if order_id in self.store.orders:
                raise OrderError(f"订单已存在: {order_id}")
            if not lines:
                raise OrderError("订单至少包含一行")
            rule_snapshot = self.rules.effective_at(placed_dt)  # 接单时刻规则快照

            stored_lines = {}
            for raw in lines:
                line = self._evaluate_line(order_id, raw, rule_snapshot, placed_dt,
                                           channel_default)
                stored_lines[line["line_id"]] = line

            order = {
                "order_id": order_id,
                "customer": customer,
                "note": note,
                "placed_at": to_iso(placed_dt),
                "rule_version_id": rule_snapshot["version_id"],
                "rule_snapshot": rule_snapshot,
                "status": "placed",
                "lines": stored_lines,
            }
            self.store.orders[order_id] = order
            self.store.order_lines[order_id] = stored_lines

            promised = [
                {"line_id": l["line_id"],
                 "bouquet_ids": list(l["bouquet_ids"]),
                 "status": l["promise"]["status"],
                 "promised_vase_days": l["promise"].get("promised_vase_days"),
                 "arrival_window": l["promise"].get("arrival_window"),
                 "reason": l["promise"].get("reason")}
                for l in stored_lines.values()
            ]
            self.store.add_decision(
                "order_placed", order_id,
                f"接单 {order_id}，适用规则 {rule_snapshot['version_id']}，"
                f"{sum(1 for l in promised if l['status'] == 'promised')} 行可承诺",
                inputs={"placed_at": to_iso(placed_dt), "customer": customer,
                        "line_ids": [p["line_id"] for p in promised]},
                outputs={"rule_version_id": rule_snapshot["version_id"], "lines": promised},
                rule_version=rule_snapshot["version_id"], at=to_iso(placed_dt),
                actor="sales",
            )
            return self.public_order(order_id)

    def _past_segments(self, prov, params, up_to_dt):
        """采切 → 预冷开始（按环境温度）+ 预冷开始 → 接单（按理想冷链温度）。"""
        shift = prov["harvest_shift"]
        ph = prov["post_harvest"]
        cut_dt = parse_iso(shift["cut_at"])
        precool_dt = parse_iso(ph["precool_started_at"])
        if precool_dt < cut_dt:
            raise OrderError("预冷开始早于采切时间，记录异常")
        segments = []
        if precool_dt > cut_dt:
            segments.append((params["ambient_temp_c"],
                             (precool_dt - cut_dt).total_seconds() / 3600.0))
        held_hours = max(0.0, (up_to_dt - precool_dt).total_seconds() / 3600.0)
        if held_hours:
            segments.append((params["ideal_chain_temp_c"], held_hours))
        return segments

    def _evaluate_bouquet(self, bouquet_id, params, rule_snapshot, placed_dt):
        prov = self.lineage.provenance(bouquet_id)
        maturity = prov["derived"]["maturity_stage"]
        past = self._past_segments(prov, params, placed_dt)
        q10, ref = params["q10"], params["reference_temp_c"]
        base_days = (params.get("base_vase_days_by_maturity")
                     or rule_snapshot["params"]["base_vase_days_by_maturity"])[str(maturity)]
        budget = coldlife.budget_hours(base_days)
        past_consumed = coldlife.consumed_hours(past, q10, ref)
        return {
            "bouquet_id": bouquet_id,
            "maturity_stage": maturity,
            "past_segments": past,
            "base_vase_days": base_days,
            "budget_hours": budget,
            "past_consumed_hours": past_consumed,
            "remaining_budget_hours": budget - past_consumed,
            "provenance": {
                "cultivar": prov["derived"]["cultivar"],
                "seed_batch_id": prov["bouquet"]["seed_batch_id"],
                "house_id": prov["bouquet"]["house_id"],
                "grower_id": prov["greenhouse"].get("grower_id"),
                "shift_id": prov["bouquet"]["shift_id"],
                "post_harvest_id": prov["bouquet"]["post_harvest_id"],
                "maturity_stage": maturity,
                "precool_wait_minutes": prov["derived"]["precool_wait_minutes"],
                "precool_minutes": prov["derived"]["precool_minutes"],
                "cut_at": prov["harvest_shift"]["cut_at"],
                "precool_started_at": prov["post_harvest"]["precool_started_at"],
                "precool_ended_at": prov["post_harvest"]["precool_ended_at"],
                "preservative": prov["post_harvest"]["preservative"],
            },
        }

    def _evaluate_line(self, order_id, raw, rule_snapshot, placed_dt, channel_default):
        line_id = raw["line_id"]
        bouquet_ids = raw.get("bouquet_ids") or ([raw["bouquet_id"]] if raw.get("bouquet_id") else [])
        if not bouquet_ids:
            raise OrderError(f"订单行 {line_id} 缺少花束")
        if len(set(bouquet_ids)) != len(bouquet_ids):
            raise OrderError(f"订单行 {line_id} 花束重复")
        channel = raw.get("channel") or channel_default
        if channel not in rule_snapshot["params"]["min_vase_days_by_channel"]:
            raise OrderError(f"未知销售渠道: {channel}")

        per_bouquet = {}
        for bouquet_id in bouquet_ids:
            if bouquet_id not in self.store.bouquets:
                raise OrderError(f"花束不存在: {bouquet_id}")
            for other_id, other in self.store.orders.items():
                for other_line in other["lines"].values():
                    if bouquet_id in other_line["bouquet_ids"]:
                        raise OrderError(
                            f"花束 {bouquet_id} 已在订单 {other_id}/"
                            f"{other_line['line_id']} 中，不能重复售卖")
            prov = self.lineage.provenance(bouquet_id)
            params = self.rules.resolve_params(rule_snapshot, prov["derived"]["cultivar"])
            per_bouquet[bouquet_id] = self._evaluate_bouquet(
                bouquet_id, params, rule_snapshot, placed_dt)
            per_bouquet[bouquet_id]["resolved_params"] = params

        # 渠道约束对所有束一致；可承诺窗口取最差束（剩余冷量最少）
        worst = min(per_bouquet.values(), key=lambda b: b["remaining_budget_hours"])
        params = worst["resolved_params"]
        min_vase = params["min_vase_days_by_channel"][channel]
        max_transit = coldlife.max_transit_hours(
            params, worst["remaining_budget_hours"], min_vase)
        max_transit_cap = params["max_transit_hours_by_channel"][channel]
        allowed_transit = min(max_transit, max_transit_cap)

        if raw.get("planned_arrival_at"):
            planned_hours = (parse_iso(raw["planned_arrival_at"]) - placed_dt).total_seconds() / 3600.0
        elif raw.get("planned_transit_hours") is not None:
            planned_hours = float(raw["planned_transit_hours"])
        else:
            planned_hours = allowed_transit
        planned_hours = max(0.0, planned_hours)

        latest_arrival = placed_dt + timedelta(hours=max(0.0, allowed_transit))
        vase_at_latest_days = coldlife.remaining_vase_days(
            worst["remaining_budget_hours"],
            coldlife.consumed_hours(
                [(params["ideal_chain_temp_c"], allowed_transit)],
                params["q10"], params["reference_temp_c"]),
        )
        promised_vase = coldlife.floor_half(vase_at_latest_days)
        tolerance = params["delay_tolerance_minutes_by_channel"][channel]

        per_bouquet_public = {
            bid: {
                "bouquet_id": bid,
                "cultivar": b["provenance"]["cultivar"],
                "maturity_stage": b["maturity_stage"],
                "base_vase_days": b["base_vase_days"],
                "past_consumed_hours": round(b["past_consumed_hours"], 3),
                "remaining_budget_hours": round(b["remaining_budget_hours"], 3),
                "planned_consumed_hours": round(coldlife.consumed_hours(
                    [(params["ideal_chain_temp_c"], planned_hours)],
                    params["q10"], params["reference_temp_c"]), 3),
            }
            for bid, b in per_bouquet.items()
        }
        promise = {
            "channel": channel,
            "min_required_vase_days": min_vase,
            "arrival_window": {
                "earliest_arrival_at": to_iso(placed_dt),
                "latest_arrival_at": to_iso(latest_arrival),
                "delay_tolerance_minutes": tolerance,
            },
            "planned_transit_hours": round(planned_hours, 2),
            "per_bouquet": per_bouquet_public,
        }
        reason = None
        if max_transit <= 0 or promised_vase < min_vase:
            reason = "剩余冷量不足以保证该渠道最低瓶插天数"
        elif planned_hours > max_transit_cap + tolerance / 60.0:
            reason = "计划在途时间超过渠道最大运输时限"
        elif planned_hours > allowed_transit:
            reason = "计划到货时间晚于可承诺到货窗口"
        promise["status"] = "rejected" if reason else "promised"
        promise["reason"] = reason
        if not reason:
            promise["promised_vase_days"] = promised_vase
            promise["committed_arrival_by"] = to_iso(
                latest_arrival + timedelta(minutes=tolerance))

        return {
            "line_id": line_id,
            "order_id": order_id,
            "bouquet_ids": list(bouquet_ids),
            "unit_price": raw.get("unit_price", raw.get("price", 0)),
            "promise": promise,
            # 接单时冻结的来源与参数：事后任何数据修改都不影响旧订单
            "frozen_provenance": {bid: b["provenance"] for bid, b in per_bouquet.items()},
            "resolved_params": {bid: b["resolved_params"] for bid, b in per_bouquet.items()},
            "receipts": [],
            "received_bouquet_ids": [],
            "incident_ids": [],
            "incidents_by_bouquet": {},
            "claim_ids": [],
            "settlement_id": None,
        }

    # ---- 查询 -----------------------------------------------------------

    def _line(self, order_id, line_id):
        lines = self.store.order_lines.get(order_id)
        if not lines or line_id not in lines:
            raise OrderError(f"订单行不存在: {order_id}/{line_id}")
        return lines[line_id]

    def public_order(self, order_id):
        with self.store.lock():
            order = self.store.orders.get(order_id)
            if not order:
                raise OrderError(f"订单不存在: {order_id}")
            return {
                "order_id": order["order_id"],
                "customer": order["customer"],
                "note": order["note"],
                "placed_at": order["placed_at"],
                "status": order["status"],
                "rule_version_id": order["rule_version_id"],
                "lines": [self._public_line(l) for l in order["lines"].values()],
            }

    @staticmethod
    def _public_line(line):
        return {
            "line_id": line["line_id"],
            "bouquet_ids": list(line["bouquet_ids"]),
            "unit_price": line["unit_price"],
            "promise": line["promise"],
            "frozen_provenance": line["frozen_provenance"],
            "resolved_params": line["resolved_params"],
            "receipts": list(line["receipts"]),
            "received_bouquet_ids": list(line["received_bouquet_ids"]),
            "incident_ids": list(line["incident_ids"]),
            "incidents_by_bouquet": {k: list(v) for k, v in line["incidents_by_bouquet"].items()},
            "claim_ids": list(line["claim_ids"]),
            "settlement_id": line["settlement_id"],
        }

    def set_status(self, order_id, status):
        with self.store.lock():
            self.store.orders[order_id]["status"] = status
