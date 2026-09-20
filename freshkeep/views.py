"""三方视图：客服、种植户、管理层各取所需，但底层事实完全一致。

- 客服视图：对客户可说明的承诺（窗口、瓶插天数、依据规则版本）与赔付依据；
- 种植户视图：按种植户汇总每笔品质扣款（预冷等待超时）与己方责任退款；
- 管理层视图：按时间回放一张订单从采切到索赔的完整决定过程。
"""

from .clock import parse_iso


class Views:
    def __init__(self, store, lineage, rules, ordering, fulfillment, monitoring):
        self.store = store
        self.lineage = lineage
        self.rules = rules
        self.ordering = ordering
        self.fulfillment = fulfillment
        self.monitoring = monitoring

    # ---- 客服 -----------------------------------------------------------

    def customer_service_view(self, order_id):
        order = self.ordering.public_order(order_id)
        lines = []
        for line in order["lines"]:
            incidents = [self.fulfillment.public_incident(i) for i in line["incident_ids"]]
            settlement = None
            if line["settlement_id"]:
                settlement = self.fulfillment.public_settlement(line["settlement_id"])
            promise = line["promise"]
            lines.append({
                "line_id": line["line_id"],
                "bouquet_ids": line["bouquet_ids"],
                "channel": promise["channel"],
                "promise": {
                    "status": promise["status"],
                    "promised_vase_days": promise.get("promised_vase_days"),
                    "committed_arrival_by": promise.get("committed_arrival_by"),
                    "arrival_window": promise.get("arrival_window"),
                    "reason_if_rejected": promise.get("reason"),
                },
                "promise_basis": {
                    "rule_version_id": order["rule_version_id"],
                    "placed_at": order["placed_at"],
                    "min_required_vase_days": promise.get("min_required_vase_days"),
                    "planned_transit_hours": promise.get("planned_transit_hours"),
                    "cold_budget_by_bouquet": promise.get("per_bouquet"),
                },
                "receipts": [
                    {"receipt_id": r["receipt_id"], "at": r["at"],
                     "bouquet_ids": r["bouquet_ids"], "condition_note": r["condition_note"]}
                    for r in line["receipts"]
                ],
                "incidents": [
                    {
                        "incident_id": i["incident_id"], "type": i["type"],
                        "severity": i["severity"], "status": i["status"],
                        "liability": i["liability"],
                        "affected_bouquets": i["affected_bouquets"],
                        "facts": i["facts"],
                        "awaiting_party": (
                            "responsible_party"
                            if i["status"] == "awaiting_evidence" else None),
                    }
                    for i in incidents
                ],
                "claim": self._claim_summary(order_id, line),
                "compensation": self._compensation_summary(line, settlement),
            })
        return {
            "order_id": order_id,
            "customer": order["customer"],
            "status": order["status"],
            "placed_at": order["placed_at"],
            "rule_version_id": order["rule_version_id"],
            "rule_note": "承诺与赔付均按接单时冻结的规则版本裁定，后续规则变更不影响本订单",
            "lines": lines,
        }

    def _claim_summary(self, order_id, public_line):
        claims = [self.fulfillment.public_claim(cid)
                  for cid in public_line["claim_ids"]]
        if not claims:
            return None
        claim = claims[0]
        return {
            "claim_id": claim["claim_id"],
            "status": claim["status"],
            "pending_incident_ids": claim.get("pending_incident_ids", []),
            "message": self._claim_message(claim["status"]),
        }

    @staticmethod
    def _claim_message(status):
        return {
            "waiting_evidence": "存在超温/延误事故，正在等待责任方补证后裁定",
            "payable": "事故已裁定，可执行赔付",
            "settled": "已完成一次性结算",
        }.get(status, status)

    @staticmethod
    def _compensation_summary(public_line, settlement):
        if not settlement:
            return None
        detail = next(
            (p for p in settlement["per_bouquet"]
             if p["bouquet_id"] in public_line["bouquet_ids"]), None)
        return {
            "settlement_id": settlement["settlement_id"],
            "settled_at": settlement["settled_at"],
            "customer_refund_total": settlement["customer_refund"],
            "refund_by_party": settlement["refund_by_party"],
            "rule_version_id": settlement["rule_version_id"],
            "rationale": settlement["rationale"],
            "sample_bouquet_detail": detail,
        }

    # ---- 种植户 ---------------------------------------------------------

    def grower_view(self, grower_id):
        rows = []
        for settlement in self.store.settlements.values():
            order = self.store.orders[settlement["order_id"]]
            line = order["lines"][settlement["line_id"]]
            grower_bouquets = [
                bid for bid in line["bouquet_ids"]
                if line["frozen_provenance"][bid].get("grower_id") == grower_id
            ]
            if not grower_bouquets:
                continue
            per_bouquet = [p for p in settlement["per_bouquet"]
                           if p["bouquet_id"] in grower_bouquets]
            price = float(line["unit_price"])
            gross = round(price * len(grower_bouquets), 2)
            precool_deduction = round(sum(
                price * p["precool_deduction_ratio"] for p in per_bouquet), 2)
            grower_refund = round(sum(
                p["refund_by_party"].get("grower", 0.0) for p in per_bouquet), 2)
            payout = round(gross - precool_deduction - grower_refund, 2)
            params = line["resolved_params"][grower_bouquets[0]]
            comp = params["compensation"]
            rows.append({
                "order_id": settlement["order_id"],
                "line_id": settlement["line_id"],
                "settlement_id": settlement["settlement_id"],
                "settled_at": settlement["settled_at"],
                "bouquet_ids": grower_bouquets,
                "gross_amount": gross,
                "deductions": {
                    "precool_wait": {
                        "amount": precool_deduction,
                        "rule": {
                            "warning_hours": params["precool_wait_warning_hours"],
                            "deduction_per_hour_ratio":
                                comp["grower_precool_deduction_per_hour"],
                            "cap_ratio": comp["grower_precool_deduction_cap"],
                            "rule_version_id": settlement["rule_version_id"],
                        },
                        "per_bouquet": [
                            {"bouquet_id": p["bouquet_id"],
                             "precool_wait_hours": p["precool_wait_hours"],
                             "deduction_ratio": p["precool_deduction_ratio"],
                             "amount": round(price * p["precool_deduction_ratio"], 2)}
                            for p in per_bouquet
                        ],
                    },
                    "quality_liability_refund": {
                        "amount": grower_refund,
                        "incidents": [
                            r for r in settlement["rationale"]
                            if r["liability"] == "grower"
                            and set(r["affected_bouquets"]) & set(grower_bouquets)
                        ],
                    },
                },
                "net_payout": payout,
            })
        total_payout = round(sum(r["net_payout"] for r in rows), 2)
        total_deduction = round(
            sum(r["deductions"]["precool_wait"]["amount"]
                + r["deductions"]["quality_liability_refund"]["amount"] for r in rows), 2)
        return {
            "grower_id": grower_id,
            "currency": "CNY",
            "settlement_count": len(rows),
            "total_net_payout": total_payout,
            "total_deductions": total_deduction,
            "rows": rows,
        }

    # ---- 管理层：决定过程回放 -------------------------------------------

    def replay_order(self, order_id):
        order = self.store.orders.get(order_id)
        if not order:
            raise ValueError(f"订单不存在: {order_id}")
        decisions = self.store.decisions_for_order(order_id)
        timeline = []
        seen_movements = set()
        # 来源事件（采切、采后、物流）放在最前，展示“从采切开始”
        for line in order["lines"].values():
            for bid in line["bouquet_ids"]:
                prov = self.lineage.provenance(bid)
                timeline.append({
                    "at": prov["harvest_shift"]["cut_at"],
                    "kind": "harvest",
                    "summary": f"{bid} 采切（{prov['derived']['cultivar']}，"
                               f"成熟度 {prov['derived']['maturity_stage']} 级）",
                    "ref": {"bouquet_id": bid, "shift_id": prov["harvest_shift"]["shift_id"]},
                })
                timeline.append({
                    "at": prov["post_harvest"]["precool_started_at"],
                    "kind": "precool",
                    "summary": f"{bid} 进入预冷，采后等待 "
                               f"{prov['derived']['precool_wait_minutes']} 分钟",
                    "ref": {"post_harvest_id": prov["post_harvest"]["record_id"]},
                })
                for e in self.lineage.movement_timeline(bid):
                    if e["event_id"] in seen_movements:
                        continue
                    seen_movements.add(e["event_id"])
                    timeline.append({
                        "at": e["at"], "kind": f"movement:{e['type']}",
                        "summary": self._movement_text(bid, e),
                        "ref": {"event_id": e["event_id"]},
                    })
        for d in decisions:
            timeline.append({
                "at": d["at"], "kind": d["kind"], "actor": d["actor"],
                "summary": d["summary"],
                "rule_version_id": d["rule_version"],
                "inputs": d["inputs"], "outputs": d["outputs"],
                "seq": d["seq"],
            })
        timeline.sort(key=lambda x: (parse_iso(x["at"]), x.get("seq", 0)))

        # 列出接单后发布的新版本，供管理层核对“旧订单未被新标准追溯”
        placed = parse_iso(order["placed_at"])
        newer_versions = [
            {"version_id": v["version_id"], "effective_from": v["effective_from"],
             "note": v["note"]}
            for v in self.rules.list_versions()
            if parse_iso(v["effective_from"]) > placed
        ]
        settlements = [self.fulfillment.public_settlement(l["settlement_id"])
                       for l in order["lines"].values() if l["settlement_id"]]
        return {
            "order_id": order_id,
            "placed_at": order["placed_at"],
            "frozen_rule_version_id": order["rule_version_id"],
            "rule_versions_published_after_order": newer_versions,
            "retroactivity_statement": "以上全部决定均依据冻结规则版本；"
                                       "其后发布的新版本仅列示，不参与旧订单的承诺与赔付",
            "timeline": timeline,
            "settlements": settlements,
        }

    @staticmethod
    def _movement_text(bid, event):
        t = event["type"]
        if t == "pack":
            return f"{bid} 拼箱入 {event['container_id']}"
        if t == "unpack":
            return f"{bid} 拆箱出 {event['container_id']}"
        if t == "load":
            return f"{event['container_id']} 装车 {event['vehicle_id']}"
        if t == "unload":
            return f"{event['container_id']} 卸车 {event['vehicle_id']}"
        if t == "transfer":
            return (f"{event['container_id']} 换冷链车：{event['from_vehicle_id']} → "
                    f"{event['to_vehicle_id']}")
        return f"{bid} 物流事件 {t}"
