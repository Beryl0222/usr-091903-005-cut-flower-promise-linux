"""冷链谱系与冷量计算。

核心口径（客服/种植户/管理层看到的是同一套数字）：

- 花束在采切时获得"瓶插期预算"（分钟），由规则中的品种基准、
  成熟度系数与采后预冷等待惩罚决定；
- 进入包装后，每一分钟按当时所处位置（箱/车）上有效温度记录器
  的读数消耗冷量，温度越高消耗越快（规则 temp_budget）；
  记录器读数之间空档超过 READING_GAP_MIN，或该位置根本没有
  记录器读数，按规则的"断连保守温度"计费——断连补传后空档
  消失，重算结果自动改变；
- 剩余冷量 = 预算 − 已消耗，接单时据此给到货窗口。
"""

from . import clock as timeutil
from .rules import band_for_temp, budget_rate

# 相邻读数超过该间隔（分钟）即视为记录器断连，空档按保守温度计费。
READING_GAP_MIN = 30


# ── 采切时质量预算 ────────────────────────────────────────────
def baseline_vase_minutes(rule, bouquet):
    """采切时的瓶插期预算（分钟）：品种 × 成熟度 − 预冷等待惩罚。"""
    cultivar = bouquet["cultivar"]
    base_hours = rule["vase_life_hours"].get(cultivar)
    if base_hours is None:
        from .errors import ValidationFailed
        raise ValidationFailed(f"规则 {rule['version']} 未覆盖品种: {cultivar}")
    factor = rule["maturity_factors"].get(bouquet.get("maturity", "commercial"), 1.0)
    penalty_hours = bouquet.get("precool_wait_min", 0) * rule["precool_penalty_per_min_hours"]
    vase_hours = base_hours * factor - penalty_hours
    return max(0, round(vase_hours * 60))


def promised_vase_minutes(rule, bouquet, remaining_min, planned_transit_min):
    """按"计划运输时长 + 理想冷藏"估出到货时瓶插期（分钟）。"""
    return max(0, round(remaining_min - planned_transit_min * budget_rate(rule, 2.0)))


# ── 位置与记录器解析 ──────────────────────────────────────────
def chain_entries(bouquet):
    return sorted(bouquet["chain"], key=lambda e: e["at"])


def accounting_start(bouquet):
    """温度计费起点：优先装箱，其次装车/采后处理/采切。"""
    for kind in ("packed", "loaded", "treated", "harvested"):
        for entry in chain_entries(bouquet):
            if entry["kind"] == kind:
                return entry["at"]
    return bouquet["cut_at"]


def _shipment_leg(state, shipment_id, at):
    shipment = state.shipments.get(shipment_id)
    if not shipment:
        return None, None
    for leg in shipment["legs"]:
        if leg["from_at"] <= at and (leg["to_at"] is None or at < leg["to_at"]):
            return shipment, leg
    # 落在最后一段之后
    return shipment, shipment["legs"][-1]


def resolve_logger(state, bouquet, at):
    """返回某分钟花束应采用的记录器 id（箱记录器优先于车记录器）。"""
    current_box = None
    shipment_id = None
    for entry in chain_entries(bouquet):
        if entry["at"] > at:
            break
        if entry["kind"] == "packed":
            current_box = entry["box_id"]
        elif entry["kind"] == "split":
            current_box = entry["box_id"]
        elif entry["kind"] == "loaded":
            shipment_id = entry["shipment_id"]
        elif entry["kind"] == "vehicle_changed":
            shipment_id = entry["shipment_id"]
    if current_box:
        box_logger = state.box_logger_at(current_box, at)
        if box_logger:
            return box_logger, "box", current_box
    if shipment_id:
        vehicle_logger = state.vehicle_logger_at(shipment_id, at)
        if vehicle_logger:
            return vehicle_logger, "vehicle", shipment_id
    return None, None, current_box


def box_members_at(state, box_id, at):
    """重建某时刻箱内花束集合（支持拼箱/拆箱历史）。"""
    box = state.boxes.get(box_id)
    if not box:
        return set()
    members = set()
    for event in sorted(box["history"], key=lambda h: h["at"]):
        if event["at"] > at:
            break
        kind = event["type"]
        if kind in ("packed", "split_in"):
            members.update(event.get("bouquet_ids", []))
        elif kind == "split_out":
            for split in event.get("splits", []):
                members.difference_update(split["bouquet_ids"])
        elif kind == "loaded":
            # 装车不改变箱内成员
            pass
    return members


def affected_bouquets_for_reading(state, logger_id, reading_at):
    """读数发生时，与该记录器同处一个箱/车的全部花束。

    拼箱、拆箱、换车后的来源保留让圈定可以精确到分钟：
    箱记录器 -> 当时箱内花束；车记录器 -> 当时车上各箱内花束。
    """
    binding = state.logger_binding_at(logger_id, reading_at)
    if not binding:
        # 车辆记录器不通过绑定事件登记，改按运输段查找
        return _affected_by_vehicle_logger(state, logger_id, reading_at)
    if binding["target_type"] == "box":
        return box_members_at(state, binding["target_id"], reading_at)
    return _affected_by_vehicle_logger(state, logger_id, reading_at)


def _affected_by_vehicle_logger(state, logger_id, at):
    affected = set()
    for shipment in state.shipments.values():
        leg = next(
            (
                leg
                for leg in shipment["legs"]
                if leg.get("logger_id") == logger_id
                and leg["from_at"] <= at
                and (leg["to_at"] is None or at < leg["to_at"])
            ),
            None,
        )
        if leg is None:
            continue
        if at < shipment["departed_at"]:
            continue
        for box_id in shipment["box_ids"]:
            affected.update(box_members_at(state, box_id, at))
    return affected


# ── 暴露历程与冷量消耗 ────────────────────────────────────────
def _temp_at_minute(state, bouquet, minute_iso, rule):
    """解析某一分钟的温度：(温度℃或None断连, 记录器id或None, 是否在途)。"""
    logger_id, target_type, _target = resolve_logger(state, bouquet, minute_iso)
    in_transit = _is_in_transit(bouquet, minute_iso)
    if not logger_id:
        return None, None, in_transit
    logger = state.loggers.get(logger_id)
    if not logger:
        return None, None, in_transit
    readings = logger["readings"]
    last_reading = None
    for reading_at in sorted(readings.keys()):
        if reading_at <= minute_iso:
            last_reading = readings[reading_at]
        else:
            break
    if last_reading is None:
        return None, logger_id, in_transit
    gap = timeutil.diff_minutes(minute_iso, last_reading["at"])
    if gap > READING_GAP_MIN:
        return None, logger_id, in_transit
    return last_reading["temp_c"], logger_id, in_transit


def _is_in_transit(bouquet, at):
    loaded_at = None
    ended_at = None
    for entry in chain_entries(bouquet):
        if entry["kind"] in ("loaded", "vehicle_changed") and loaded_at is None:
            loaded_at = entry["at"]
        if entry["kind"] in ("shipment_arrived", "received"):
            ended_at = entry["at"]
    if loaded_at is None:
        return False
    return at >= loaded_at and (ended_at is None or at < ended_at)


def exposure(state, rule, bouquet, until):
    """计算花束从计费起点到 until 的温度暴露与冷量消耗。"""
    start = accounting_start(bouquet)
    if until <= start:
        return _empty_exposure(start, until)
    total_min = timeutil.minutes_between(start, until)
    band_minutes = {band["name"]: 0 for band in rule["temp_bands"]}
    consumed = 0.0
    covered_transit = 0
    disconnected_transit = 0
    disconnected_storage = 0
    excursion_windows = []
    current_window = None

    for offset in range(total_min):
        minute = timeutil.add_minutes(start, offset)
        temp_c, logger_id, in_transit = _temp_at_minute(state, bouquet, minute, rule)
        if temp_c is None:
            temp_c = rule["disconnect_temp_c"]
            band = band_for_temp(rule, temp_c)
            if in_transit:
                disconnected_transit += 1
            else:
                disconnected_storage += 1
        else:
            band = band_for_temp(rule, temp_c)
            if in_transit:
                covered_transit += 1
        band_minutes[band["name"]] += 1
        consumed += budget_rate(rule, temp_c)
        if band.get("excursion") and temp_c is not None:
            if current_window is None:
                current_window = {"start": minute, "end": minute, "max_temp_c": temp_c}
            else:
                current_window["end"] = minute
                current_window["max_temp_c"] = max(current_window["max_temp_c"], temp_c)
        elif current_window is not None:
            excursion_windows.append(current_window)
            current_window = None
    if current_window is not None:
        excursion_windows.append(current_window)

    transit_minutes = covered_transit + disconnected_transit
    return {
        "from": start,
        "until": until,
        "timeline_min": total_min,
        "consumed_min": round(consumed),
        "band_minutes": band_minutes,
        "covered_transit_min": covered_transit,
        "disconnected_transit_min": disconnected_transit,
        "disconnected_storage_min": disconnected_storage,
        "transit_min": transit_minutes,
        "connectivity_ratio": round(covered_transit / transit_minutes, 3) if transit_minutes else 1.0,
        "excursion_windows": excursion_windows,
    }


def _empty_exposure(start, until):
    return {
        "from": start,
        "until": until,
        "timeline_min": 0,
        "consumed_min": 0,
        "band_minutes": {},
        "covered_transit_min": 0,
        "disconnected_transit_min": 0,
        "disconnected_storage_min": 0,
        "transit_min": 0,
        "connectivity_ratio": 1.0,
        "excursion_windows": [],
    }


def cool_state(state, rule, bouquet, until):
    """预算、已消耗、剩余与到货预估瓶插期的完整口径。"""
    budget = baseline_vase_minutes(rule, bouquet)
    exp = exposure(state, rule, bouquet, until)
    remaining = max(0, budget - exp["consumed_min"])
    return {
        "bouquet_id": bouquet["id"],
        "budget_min": budget,
        "consumed_min": exp["consumed_min"],
        "remaining_min": remaining,
        "remaining_vase_hours": round(remaining / 60, 1),
        "exposure": exp,
    }
