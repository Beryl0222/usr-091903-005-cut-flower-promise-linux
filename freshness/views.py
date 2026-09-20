"""面向三类角色的只读视图。

同一事件流，不同角色看到不同的解释口径：
- 客服：可说明的承诺与赔付依据；
- 种植户：可核对的品质扣款（只承担产地责任）；
- 管理层：订单从采切到索赔的完整决定回放。
"""

from . import clock as timeutil
from . import coldchain


def _lineage(state, bouquet):
    shift = state.shifts.get(bouquet["shift_id"], {})
    batch = state.seed_batches.get(bouquet["seed_batch_id"], {})
    house = state.greenhouses.get(bouquet["greenhouse_id"], {})
    return {
        "seed_batch_id": bouquet["seed_batch_id"],
        "cultivar": bouquet["cultivar"],
        "seed_supplier": batch.get("supplier"),
        "greenhouse_id": bouquet["greenhouse_id"],
        "greenhouse_name": house.get("name"),
        "shift_id": bouquet["shift_id"],
        "cut_at": bouquet["cut_at"],
        "maturity": bouquet.get("maturity"),
        "precool_wait_min": bouquet.get("precool_wait_min", 0),
        "grading": bouquet.get("grading"),
        "treatments": [state.treatments[t] for t in bouquet.get("treatment_ids", [])],
    }


def _observable_until(state, bouquet, order, wall_now):
    """估算口径时刻：已签收取签收；否则取最新一条可观测数据时刻，
    绝不用墙钟把"尚无数据的未来区间"按保守温度烧掉。"""
    candidates = [order["accepted_at"]]
    if bouquet.get("received_at"):
        return bouquet["received_at"]
    shipment = bouquet.get("current_shipment")
    if shipment and state.shipments.get(shipment, {}).get("arrived_at"):
        candidates.append(state.shipments[shipment]["arrived_at"])
    # 花束当前/历史箱与运输段上的记录器最新读数
    loggers = set()
    for entry in bouquet["chain"]:
        box_id = entry.get("box_id") or entry.get("from_box_id")
        if box_id:
            for logger_id, logger in state.loggers.items():
                if any(b["target_type"] == "box" and b["target_id"] == box_id
                       for b in logger["bindings"]):
                    loggers.add(logger_id)
    if shipment:
        for leg in state.shipments.get(shipment, {}).get("legs", []):
            if leg.get("logger_id"):
                loggers.add(leg["logger_id"])
    for logger_id in loggers:
        logger = state.loggers.get(logger_id)
        if logger and logger["readings"]:
            candidates.append(max(logger["readings"].keys()))
    latest = max(candidates)
    return min(latest, wall_now) if wall_now else latest


def customer_service_view(service, order_id):
    """客服视图：这单承诺了什么、为什么赔、赔多少。"""
    state = service.state
    order = state.order(order_id)
    rule = order["rule_snapshot"]
    now = timeutil.now(service.clock)

    bouquets_view = []
    for bid in order["bouquet_ids"]:
        bouquet = state.bouquets[bid]
        commitment = order["commitments"][bid]
        estimate_at = _observable_until(state, bouquet, order, now)
        cool = coldchain.cool_state(state, rule, bouquet, estimate_at)
        bouquets_view.append({
            "bouquet_id": bid,
            "lineage": _lineage(state, bouquet),
            "promised_arrival_window": order["arrival_window"],
            "promised_vase_hours": commitment["promised_vase_hours"],
            "estimated_as_of": estimate_at,
            "current_estimated_vase_hours": cool["remaining_vase_hours"],
            "received_at": bouquet.get("received_at"),
            "receipt_condition": bouquet.get("receipt_condition"),
            "basis": (
                f"按接单时固化的 {rule['version']}：品种基准 "
                f"{rule['vase_life_hours'][bouquet['cultivar']]} 小时 × "
                f"成熟度系数 {rule['maturity_factors'].get(bouquet.get('maturity'), 1.0)}，"
                f"预冷等待 {bouquet.get('precool_wait_min', 0)} 分钟已扣减；"
                f"在途温度按分钟折减冷量"
            ),
        })

    flags = [f for f in state.flags.values() if f["order_id"] == order_id]
    flags_view = [{
        "flag_id": f["id"],
        "reason": f["reason"],
        "reason_text": REASON_TEXT.get(f["reason"], f["reason"]),
        "status": f["status"],
        "status_text": STATUS_TEXT.get(f["status"], f["status"]),
        "affected_bouquets": f["bouquet_ids"],
        "window": f.get("window"),
        "detail": f["detail"],
        "liable_party": f.get("liable_party"),
        "evidence": f.get("evidence", []),
    } for f in sorted(flags, key=lambda x: x["opened_at"])]

    settlement = state.settlements.get(order_id)
    result = {
        "order_id": order_id,
        "channel": order["channel"],
        "customer": order["customer"],
        "destination": order["destination"],
        "status": order["status"],
        "accepted_at": order["accepted_at"],
        "rule_version_frozen_at_acceptance": rule["version"],
        "rule_note": rule.get("note", ""),
        "rule_freeze_explanation": (
            f"本单于 {order['accepted_at']} 接单，承诺永久按当时有效的 "
            f"{rule['version']} 解释；之后发布的放宽/收紧版本不影响本单。"
        ),
        "promised_arrival_window": order["arrival_window"],
        "bouquets": bouquets_view,
        "exceptions": flags_view,
        "open_exceptions": [f["id"] for f in flags if f["status"] not in ("ruled", "cleared")],
        "settlement": None,
    }
    if settlement:
        result["settlement"] = {
            "settled_at": settlement["settled_at"],
            "customer_payout_total": settlement["customer_payout_total"],
            "per_bouquet": [{
                "bouquet_id": line["bouquet_id"],
                "promised_vase_hours": line["promised_vase_hours"],
                "actual_vase_hours": line["actual_vase_hours"],
                "shortfall_ratio": line["shortfall_ratio"],
                "compensation_ratio": line["compensation_ratio"],
                "payout": line["customer_payout"],
                "reasons": line["reasons"],
            } for line in settlement["lines"] if line["customer_payout"] > 0
               or line["reasons"]],
            "payout_explanation": (
                f"按 {rule['version']} 赔付档位，以每束花实际瓶插期相对承诺值的"
                f"缩水比例取档；超温、断连、延误经责任方补证并终裁后，"
                f"赔付由责任方承担。"
            ),
        }
    return result


def grower_view(service, greenhouse_id=None, seed_batch_id=None):
    """种植户视图：每一笔品质扣款都能核对到品种/成熟度/预冷数据。"""
    state = service.state
    rows = []
    for order_id, settlement in state.settlements.items():
        order = state.orders[order_id]
        for line in settlement["lines"]:
            bouquet = state.bouquets[line["bouquet_id"]]
            if greenhouse_id and bouquet["greenhouse_id"] != greenhouse_id:
                continue
            if seed_batch_id and bouquet["seed_batch_id"] != seed_batch_id:
                continue
            rule = order["rule_snapshot"]
            standard_hours = rule["vase_life_hours"][bouquet["cultivar"]]
            baseline_hours = round(
                coldchain.baseline_vase_minutes(rule, bouquet) / 60, 1
            )
            carrier_reasons = [r for r in line["reasons"]
                               if r.get("outcome") == "ruled"
                               and r.get("liable_party") == "carrier"]
            rows.append({
                "order_id": order_id,
                "settled_at": settlement["settled_at"],
                "bouquet_id": bouquet["id"],
                "greenhouse_id": bouquet["greenhouse_id"],
                "seed_batch_id": bouquet["seed_batch_id"],
                "shift_id": bouquet["shift_id"],
                "cultivar": bouquet["cultivar"],
                "cut_at": bouquet["cut_at"],
                "maturity": bouquet.get("maturity"),
                "precool_wait_min": bouquet.get("precool_wait_min", 0),
                "grading": bouquet.get("grading"),
                "rule_version": rule["version"],
                "standard_vase_hours": standard_hours,
                "origin_baseline_hours": baseline_hours,
                "actual_vase_hours": line["actual_vase_hours"],
                "deduction": line["grower_deduction"],
                "deduction_reason": (
                    "产地责任：成熟度/预冷等待导致基线低于品种标准 ≥10%，"
                    "且无承运方责任" if line["grower_deduction"] > 0 else
                    "免于产地扣款：运输环节异常由承运方承担" if carrier_reasons else
                    "瓶插期缩水未达赔付档位"
                ),
                "carrier_faults": [r["reason"] for r in carrier_reasons],
            })
    deductions = [r for r in rows if r["deduction"] > 0]
    return {
        "greenhouse_id": greenhouse_id,
        "seed_batch_id": seed_batch_id,
        "settled_lines": rows,
        "deduction_lines": deductions,
        "deduction_total": round(sum(r["deduction"] for r in deductions), 2),
        "explanation": (
            "扣款只认产地自身因素：品种基准 × 成熟度系数 − 预冷等待惩罚得出的"
            "基线瓶插期低于品种标准 10% 以上才计扣；凡已裁定为承运方责任的"
            "超温、断连、延误，不计入种植户扣款。接单时的规则版本即扣款口径。"
        ),
    }


def management_view(service):
    """管理层视图：订单总览 + 规则演进 + 异常闭环情况。"""
    state = service.state
    orders = []
    for order_id, order in sorted(state.orders.items()):
        flags = [f for f in state.flags.values() if f["order_id"] == order_id]
        orders.append({
            "order_id": order_id,
            "status": order["status"],
            "accepted_at": order["accepted_at"],
            "rule_version": order["rule_version"],
            "channel": order["channel"],
            "amount": order["amount"],
            "window": order["arrival_window"],
            "open_flags": sum(1 for f in flags if f["status"] not in ("ruled", "cleared")),
            "settled": order_id in state.settlements,
            "payout": state.settlements[order_id]["customer_payout_total"]
                      if order_id in state.settlements else None,
        })
    return {
        "orders": orders,
        "rule_versions": service.rulebook.all_versions(),
        "exceptions": [
            {
                "flag_id": f["id"], "order_id": f["order_id"], "reason": f["reason"],
                "status": f["status"], "liable_party": f.get("liable_party"),
                "bouquet_count": len(f["bouquet_ids"]), "opened_at": f["opened_at"],
            }
            for f in sorted(state.flags.values(), key=lambda x: x["opened_at"])
        ],
        "settlements": [
            {"order_id": oid, **{k: s[k] for k in
                                 ("customer_payout_total", "grower_deduction_total",
                                  "carrier_penalty_total", "settled_at")}}
            for oid, s in sorted(state.settlements.items())
        ],
        "event_count": len(service.store.all()),
    }


REASON_TEXT = {
    "temperature_excursion": "在途超温",
    "logger_disconnect": "温度记录器断连",
    "late_delivery": "到货/签收延误",
    "arrival_quality": "到货品相异常",
}

STATUS_TEXT = {
    "open": "待责任方补证",
    "evidence_received": "已补证，待核/重评",
    "ruled": "已终裁定责",
    "cleared": "补证后自动解除",
}
