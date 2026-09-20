"""端到端业务演示：一筐花从玉溪采切到三类销售端索赔的全过程。

运行：python3 demo.py
"""

import json

from freshness.app import FreshnessService
from freshness.clock import add_minutes
from freshness import views


def readings(logger_id, start, hours, temp, step=10):
    return [
        {"logger_id": logger_id, "at": add_minutes(start, m), "temp_c": temp}
        for m in range(0, hours * 60 + 1, step)
    ]


def show(title, data):
    print(f"\n{'─' * 68}\n{title}\n{'─' * 68}")
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main():
    svc = FreshnessService()
    T0 = "2026-09-05T08:00:00"

    # 1) 产地建档：两个种苗批次、两个棚、一个早班
    svc.register_seed_batch("SB-KL", "卡罗拉", supplier="玉溪种苗A场", at=T0)
    svc.register_seed_batch("SB-YX", "影星", supplier="玉溪种苗B场", at=T0)
    svc.register_greenhouse("GH-1", name="一号棚", at=T0)
    svc.register_greenhouse("GH-2", name="二号棚", at=T0)
    svc.perform_cut_shift("SH-AM", "GH-1", "SB-KL", shift_code="早班", at=T0)
    svc.perform_cut_shift("SH-PM", "GH-2", "SB-YX", shift_code="早班", at=T0)
    # 6 束花：采切即继承 种苗批次/棚/班次
    svc.harvest_bouquets("SH-AM", ["B1", "B2", "B3"], maturity="commercial", at=T0)
    svc.harvest_bouquets("SH-PM", ["B4", "B5", "B6"], maturity="tight_bud", at=T0)
    # 采后记录：B5/B6 所在批次预冷等待 90 分钟（产地隐患）
    svc.record_postharvest(["B1", "B2", "B3"], precool_wait_min=15,
                           grading="A", note="及时预冷", at=add_minutes(T0, 30))
    svc.record_postharvest(["B4", "B5", "B6"], precool_wait_min=90,
                           grading="B", note="预冷排队", at=add_minutes(T0, 30))

    # 2) 拼箱（6 束两品种同筐）-> 按三个销售渠道拆箱，来源不断
    svc.pack_box("BOX-ALL", ["B1", "B2", "B3", "B4", "B5", "B6"],
                 logger_id="LG-A", at=add_minutes(T0, 60))
    svc.split_box("BOX-ALL", [
        {"box_id": "BOX-WS", "bouquet_ids": ["B1", "B4"]},   # 批发市场
        {"box_id": "BOX-WED", "bouquet_ids": ["B2", "B5"]},  # 婚礼现场
        {"box_id": "BOX-FLR", "bouquet_ids": ["B3", "B6"]},  # 城市花店
    ], at=add_minutes(T0, 90))
    for box, logger in (("BOX-WS", "LG-WS"), ("BOX-WED", "LG-WED"), ("BOX-FLR", "LG-FLR")):
        svc.bind_logger(logger, "box", box, at=add_minutes(T0, 90))

    # 3) 装车发运，10:00 三单同时接单（当时 v1 规则有效）
    dep = add_minutes(T0, 120)
    svc.create_shipment("TR-1", "云冷链", ["BOX-WS", "BOX-WED", "BOX-FLR"],
                        "V-滇A001", vehicle_logger_id="LG-V1",
                        route="玉溪-昆明", at=dep)
    svc.accept_order("ORD-WS", ["B1", "B4"], 600.0, channel="wholesale",
                     customer="斗南批发市场", idempotency_key="ord-ws", at=dep)
    svc.accept_order("ORD-WED", ["B2", "B5"], 1200.0, channel="wedding",
                     customer="洲际酒店婚礼", idempotency_key="ord-wed", at=dep)
    svc.accept_order("ORD-FLR", ["B3", "B6"], 800.0, channel="florist",
                     customer="城市花店联盟", idempotency_key="ord-flr", at=dep)
    print("接单完成：三单均固化规则版本 rule-v1-2026-08 与每束花冷量快照")

    # 4) 管理层 9 月 5 日预告：9 月 15 日起执行放宽赔付档（只对新单生效，
    #    三单已在 9 月 5 日固化 v1，永远不按放宽档解释）
    relaxed = dict(svc.rulebook.get("rule-v1-2026-08"))
    relaxed.pop("version")
    relaxed["compensation"] = [
        {"shortfall_min": 0.30, "pay_ratio": 0.15},
        {"shortfall_min": 0.20, "pay_ratio": 0.08},
    ]
    svc.publish_rule("rule-v1b-relaxed", "2026-09-15T00:00:00", relaxed,
                     note="淡季放宽赔付档，不溯及在途订单", at="2026-09-05T15:00:00")

    # 5) 温度数据（箱记录器 09:00 绑定起就有读数）
    bind_at = add_minutes(T0, 90)
    #  拼箱记录器覆盖拆箱前的仓储段
    svc.record_temperature_batch(readings("LG-A", add_minutes(T0, 60), 1, 3.0), at=dep)
    #  批发市场箱：发车后冷藏机故障，整段在途 18℃
    svc.record_temperature_batch(readings("LG-WS", bind_at, 1, 3.0), at=dep)
    hot = [{"logger_id": "LG-WS", "at": add_minutes(dep, m), "temp_c": 18.0}
           for m in range(10, 361, 10)]
    svc.record_temperature_batch(hot, at=add_minutes(dep, 360))
    #  花店箱：全程 3℃
    svc.record_temperature_batch(readings("LG-FLR", bind_at, 7, 3.0), at=dep)
    #  婚礼箱：12:00 后记录器断连（先只传到 12:00）
    svc.record_temperature_batch(readings("LG-WED", bind_at, 3, 3.0), at=dep)
    #  车厢记录器（被箱记录器遮蔽，留作冗余）
    svc.record_temperature_batch(readings("LG-V1", dep, 6, 4.0), at=dep)

    # 6) 到达：婚礼箱 12:00-16:00 无覆盖 -> 自动圈定断连，等待承运方补证
    d1 = add_minutes(dep, 6 * 60)
    svc.shipment_arrived("TR-1", arrived_at=d1, at=d1)
    print("\n到达后系统自动圈定的异常：")
    for f in svc.list_flags():
        print(f"  [{f['status']}] {f['order_id']} {f['reason']} "
              f"花束={f['bouquet_ids']}")

    # 7) 承运方补传婚礼箱断连区间的冷藏读数 -> 标志自动解除；重复补传只算一次
    backfill = readings("LG-WED", add_minutes(dep, 120), 4, 3.0)
    first = svc.record_temperature_batch(backfill, at=add_minutes(d1, 30))
    again = svc.record_temperature_batch(backfill, at=add_minutes(d1, 31))
    print(f"\n断连补传：首次 {first}，重复推送 {again}")
    wed_flag = next(f for f in svc.list_flags("ORD-WED") if f["reason"] == "logger_disconnect")
    print(f"婚礼单断连标志状态：{wed_flag['status']}（{wed_flag['history'][-1]['detail']}）")

    # 8) 分批签收
    svc.confirm_receipt("ORD-WS", ["B1", "B4"], d1, shipment_id="TR-1", at=d1)
    svc.confirm_receipt("ORD-WED", ["B2", "B5"], d1, shipment_id="TR-1", at=d1)
    svc.confirm_receipt("ORD-FLR", ["B3"], d1, shipment_id="TR-1", at=d1)
    # B6 蔫萎投诉（与产地 90 分钟预冷等待吻合）
    svc.confirm_receipt("ORD-FLR", ["B6"], d1, condition="wilted",
                        shipment_id="TR-1", note="签收时外层花瓣萎蔫", at=d1)

    # 9) 补证与终裁
    hot_flag = next(f for f in svc.list_flags("ORD-WS") if f["reason"] == "temperature_excursion")
    svc.submit_evidence(hot_flag["id"], "carrier", kind="repair_note",
                        note="发车后冷藏机持续故障，途中报修未能恢复，附维修单", at=d1)
    svc.rule_flag(hot_flag["id"], "carrier", "carrier_liable",
                  reason="真实读数坐实在途超温，由承运方承担", at=d1)
    qual_flag = next(f for f in svc.list_flags("ORD-FLR") if f["reason"] == "arrival_quality")
    svc.submit_evidence(qual_flag["id"], "grower", kind="precool_log",
                        note="该批次预冷排队 90 分钟，采后记录可查", at=d1)
    svc.rule_flag(qual_flag["id"], "grower", "grower_liable",
                  reason="预冷等待过长导致到货品相不足，属产地责任", at=d1)

    # 10) 一次结算
    for order_id in ("ORD-WS", "ORD-WED", "ORD-FLR"):
        result = svc.settle_order(order_id, at=d1)
        show(f"结算 {order_id}（按 {result['rule_version']}）", {
            "客户赔付": result["customer_payout_total"],
            "种植户扣款": result["grower_deduction_total"],
            "承运方罚金": result["carrier_penalty_total"],
            "明细": [
                {k: line[k] for k in ("bouquet_id", "promised_vase_hours",
                                      "actual_vase_hours", "shortfall_ratio",
                                      "compensation_ratio", "customer_payout",
                                      "grower_deduction", "carrier_penalty")}
                for line in result["lines"]
            ],
        })

    # 11) 三个角色视图
    cs = views.customer_service_view(svc, "ORD-WS")
    show("客服视图（批发市场单）：可说明的承诺依据", {
        "规则固化说明": cs["rule_freeze_explanation"],
        "承诺窗口": cs["promised_arrival_window"],
        "异常": [{"原因": e["reason_text"], "状态": e["status_text"],
                  "责任方": e["liable_party"]} for e in cs["exceptions"]],
        "赔付总额": cs["settlement"]["customer_payout_total"],
    })
    grower = views.grower_view(svc)
    show("种植户视图：可核对的品质扣款", {
        "扣款总额": grower["deduction_total"],
        "扣款明细": [{"订单": r["order_id"], "花束": r["bouquet_id"],
                     "预冷等待(分)": r["precool_wait_min"],
                     "扣款": r["deduction"], "依据": r["deduction_reason"]}
                    for r in grower["deduction_lines"]],
    })
    replay = svc.replay("ORD-FLR")
    show("管理层视图：花店单从采切到索赔的完整回放", {
        "事件数": len(replay["timeline"]),
        "决定过程": [f"{e['at']}  {e['type']}  ({e['actor']})"
                    for e in replay["timeline"]],
    })


if __name__ == "__main__":
    main()
