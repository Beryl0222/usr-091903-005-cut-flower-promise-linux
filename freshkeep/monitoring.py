"""温度采集：断连补传与重复读数一律安全。

设计原则：
- 读数按 (logger_id, seq) 去重，同一批次重传（承运方重复回调）不产生重复数据；
- 断连只登记“缺口”，补传读数按时间插回原序列，分析永远基于合并后的完整时间线；
- 缺口未被补传或无有效说明前，不能假设冷链正常——该事实交给异常判定处理。
本服务只保管事实（读数与缺口），用什么标准判定由订单冻结的规则决定。
"""

from .clock import now_iso, parse_iso, to_iso


class TemperatureError(ValueError):
    pass


class TemperatureService:
    def __init__(self, store, lineage, clock=None):
        self.store = store
        self.lineage = lineage
        self.clock = clock

    def _at(self, at=None):
        return to_iso(at) if at else (self.clock.now_iso() if self.clock else now_iso())

    def record_reading(self, logger_id, seq, read_at, temp_c, event_id=None):
        """上报一条温度读数。event_id 或 (logger_id, seq) 命中均视为重复。"""
        read_at = to_iso(read_at)
        with self.store.lock():
            series = self.store.temp_readings.setdefault(logger_id, {})
            if event_id:
                for existing in series.values():
                    if existing.get("event_id") == event_id:
                        return {"reading": existing, "deduplicated": True, "reason": "event_id"}
            key = str(seq)
            if key in series:
                return {"reading": series[key], "deduplicated": True, "reason": "seq"}
            reading = {
                "logger_id": logger_id, "seq": seq, "read_at": read_at,
                "temp_c": float(temp_c), "event_id": event_id,
                "backfilled": False, "recorded_at": self._at(),
            }
            series[key] = reading
            return {"reading": reading, "deduplicated": False}

    def report_gap(self, logger_id, gap_id, disconnected_at, resumed_at=None, reason=None):
        """记录仪主动上报断连（恢复时间可后补）。幂等。"""
        disconnected_at = to_iso(disconnected_at)
        resumed_at = to_iso(resumed_at) if resumed_at else None
        with self.store.lock():
            gaps = self.store.temp_gaps.setdefault(logger_id, {})
            if gap_id in gaps:
                return {"gap": gaps[gap_id], "deduplicated": True}
            gap = {
                "logger_id": logger_id, "gap_id": gap_id,
                "disconnected_at": disconnected_at, "resumed_at": resumed_at,
                "reason": reason, "status": "open",  # open | backfilled | explained
                "backfill_event_id": None,
                "created_at": self._at(),
            }
            gaps[gap_id] = gap
            return {"gap": gap, "deduplicated": False}

    def close_gap(self, logger_id, gap_id, resumed_at=None):
        with self.store.lock():
            gap = self.store.temp_gaps[logger_id][gap_id]
            if resumed_at:
                gap["resumed_at"] = to_iso(resumed_at)
            return {"gap": gap, "deduplicated": False}

    def backfill_readings(self, logger_id, readings, gap_id=None, event_id=None):
        """断连恢复后补传一批读数，按 seq 合并进原时间线。整批幂等。"""
        with self.store.lock():
            series = self.store.temp_readings.setdefault(logger_id, {})
            if event_id:
                for existing in series.values():
                    if existing.get("backfill_event_id") == event_id:
                        return {"gap_id": gap_id, "deduplicated": True,
                                "inserted": 0, "reason": "event_id"}
            inserted = 0
            for r in readings:
                key = str(r["seq"])
                if key in series:
                    continue
                series[key] = {
                    "logger_id": logger_id, "seq": r["seq"],
                    "read_at": to_iso(r["read_at"]), "temp_c": float(r["temp_c"]),
                    "event_id": r.get("event_id"), "backfilled": True,
                    "backfill_event_id": event_id, "recorded_at": self._at(),
                }
                inserted += 1
            gap = None
            if gap_id:
                gaps = self.store.temp_gaps.setdefault(logger_id, {})
                gap = gaps.get(gap_id)
                if gap is None:
                    raise TemperatureError(f"断连记录不存在: {logger_id}/{gap_id}")
                if inserted or gap["status"] == "open":
                    gap["status"] = "backfilled"
                    gap["backfill_event_id"] = event_id
                    if not gap.get("resumed_at") and readings:
                        gap["resumed_at"] = to_iso(max(r["read_at"] for r in readings))
            return {"gap_id": gap_id, "deduplicated": inserted == 0, "inserted": inserted,
                    "gap": gap}

    def add_gap_explanation(self, logger_id, gap_id, explanation, evidence_ref=None, by=None):
        """责任方对缺口提交情况说明（补证的一种：设备故障证明等）。"""
        with self.store.lock():
            gap = self.store.temp_gaps[logger_id][gap_id]
            gap.setdefault("explanations", []).append({
                "explanation": explanation, "evidence_ref": evidence_ref,
                "by": by, "at": self._at(),
            })
            gap["status"] = "explained"
            return {"gap": gap}

    # ---- 查询：只提供事实时间线 -----------------------------------------

    def timeline(self, logger_id, start=None, end=None):
        """合并补传后的完整读数时间线。"""
        with self.store.lock():
            readings = sorted(self.store.temp_readings.get(logger_id, {}).values(),
                              key=lambda r: (parse_iso(r["read_at"]), r["seq"]))
        if start:
            start = parse_iso(start)
            readings = [r for r in readings if parse_iso(r["read_at"]) >= start]
        if end:
            end = parse_iso(end)
            readings = [r for r in readings if parse_iso(r["read_at"]) <= end]
        return readings

    def gaps(self, logger_id, start=None, end=None):
        with self.store.lock():
            gaps = list(self.store.temp_gaps.get(logger_id, {}).values())
        if start or end:
            win_start = parse_iso(start) if start else None
            win_end = parse_iso(end) if end else None

            def overlaps(g):
                g_start = parse_iso(g["disconnected_at"])
                g_end = parse_iso(g["resumed_at"]) if g.get("resumed_at") else None
                if win_end and g_start > win_end:
                    return False
                if win_start and g_end is not None and g_end < win_start:
                    return False
                return True

            gaps = [g for g in gaps if overlaps(g)]
        return sorted(gaps, key=lambda g: g["disconnected_at"])

    def readings_for_bouquet(self, bouquet_id, start, end):
        """收集某时间窗内、跟随过该花束所在箱的全部记录仪读数。

        每条读数归属判定：读数时刻花束所在箱，与该记录仪当时绑定的箱一致。
        """
        start, end = parse_iso(start), parse_iso(end)
        result = []
        with self.store.lock():
            bindings = dict(self.store.logger_bindings)
        for logger_id, binding in bindings.items():
            bound_box = binding["container_id"]
            for r in self.timeline(logger_id, start, end):
                t = parse_iso(r["read_at"])
                box = self.lineage.container_of_bouquet_at(bouquet_id, t)
                if box == bound_box:
                    result.append(dict(r))
        result.sort(key=lambda r: (parse_iso(r["read_at"]), r["seq"]))
        return result
