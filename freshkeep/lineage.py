"""溯源服务：建档来源不可变，物流只追加事件，支持任意时刻的归属查询。

拼箱（pack）、装箱上车（load）、换车（transfer）、卸货（unload）、拆箱（unpack）
都是追加事件。花束的来源档案在创建时固化，任何搬运都不会覆盖它；
温度超温圈定花束时，按“异常时间窗内该载具子树中实际装载的花束”计算。
"""

from .clock import now_iso, parse_iso, to_iso


class LineageError(ValueError):
    pass


class LineageService:
    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock

    def _at(self, at):
        return parse_iso(at) if at else (parse_iso(self.clock.now_iso()) if self.clock else parse_iso(now_iso()))

    # ---- 建档 ----------------------------------------------------------

    def register_seed_batch(self, batch_id, cultivar, **kwargs):
        from .models import SeedBatch

        with self.store.lock():
            if batch_id in self.store.seed_batches:
                raise LineageError(f"种苗批次已存在: {batch_id}")
            obj = SeedBatch(batch_id, cultivar, **kwargs)
            self.store.seed_batches[batch_id] = obj
            return obj.to_dict()

    def register_greenhouse(self, house_id, name=None, location=None, grower_id=None):
        from .models import Greenhouse

        with self.store.lock():
            if house_id in self.store.greenhouses:
                raise LineageError(f"种植棚已存在: {house_id}")
            obj = Greenhouse(house_id, name, location, grower_id)
            self.store.greenhouses[house_id] = obj
            return obj.to_dict()

    def register_shift(self, shift_id, house_id, cut_at, maturity_stage, **kwargs):
        from .models import HarvestShift

        with self.store.lock():
            if shift_id in self.store.shifts:
                raise LineageError(f"采切班次已存在: {shift_id}")
            if house_id not in self.store.greenhouses:
                raise LineageError(f"种植棚不存在: {house_id}")
            obj = HarvestShift(shift_id, house_id, to_iso(cut_at), maturity_stage, **kwargs)
            self.store.shifts[shift_id] = obj
            return obj.to_dict()

    def register_post_harvest(self, record_id, shift_id, precool_started_at,
                              precool_ended_at, **kwargs):
        from .models import PostHarvestRecord

        with self.store.lock():
            if record_id in self.store.post_harvest:
                raise LineageError(f"采后处理记录已存在: {record_id}")
            if shift_id not in self.store.shifts:
                raise LineageError(f"采切班次不存在: {shift_id}")
            obj = PostHarvestRecord(
                record_id, shift_id, to_iso(precool_started_at), to_iso(precool_ended_at), **kwargs
            )
            self.store.post_harvest[record_id] = obj
            return obj.to_dict()

    def register_bouquet(self, bouquet_id, seed_batch_id, house_id, shift_id,
                         post_harvest_id, stems=1, created_at=None):
        from .models import Bouquet

        with self.store.lock():
            if bouquet_id in self.store.bouquets:
                raise LineageError(f"花束已存在: {bouquet_id}")
            for kind, key, table in (
                ("种苗批次", seed_batch_id, self.store.seed_batches),
                ("种植棚", house_id, self.store.greenhouses),
                ("采切班次", shift_id, self.store.shifts),
                ("采后处理记录", post_harvest_id, self.store.post_harvest),
            ):
                if key not in table:
                    raise LineageError(f"{kind}不存在: {key}")
            shift = self.store.shifts[shift_id]
            ph = self.store.post_harvest[post_harvest_id]
            if shift.house_id != house_id:
                raise LineageError("采切班次与种植棚不一致，来源链断裂")
            if ph.shift_id != shift_id:
                raise LineageError("采后处理记录与采切班次不一致，来源链断裂")
            obj = Bouquet(
                bouquet_id, seed_batch_id, house_id, shift_id, post_harvest_id,
                stems=stems, created_at=to_iso(created_at) if created_at else None,
            )
            self.store.bouquets[bouquet_id] = obj
            return obj.to_dict()

    def register_container(self, container_id, kind, capacity=None):
        from .models import ColdContainer

        with self.store.lock():
            if container_id in self.store.containers:
                raise LineageError(f"载具已存在: {container_id}")
            obj = ColdContainer(container_id, kind, capacity)
            self.store.containers[container_id] = obj
            return obj.to_dict()

    # ---- 物流事件（幂等追加） -------------------------------------------

    def _append_event(self, event_id, event):
        if event_id in self.store.lineage_events:
            return self.store.lineage_events[event_id], False
        event["event_id"] = event_id
        self.store.lineage_events[event_id] = event
        self.store.lineage_order.append(event_id)
        return event, True

    def pack(self, event_id, box_id, bouquet_ids, at=None):
        """拼箱：花束装入箱。"""
        at = to_iso(self._at(at))
        with self.store.lock():
            if box_id not in self.store.containers:
                raise LineageError(f"载具不存在: {box_id}")
            for b in bouquet_ids:
                if b not in self.store.bouquets:
                    raise LineageError(f"花束不存在: {b}")
            event, created = self._append_event(event_id, {
                "type": "pack", "at": at, "container_id": box_id, "bouquet_ids": list(bouquet_ids),
            })
            return {"event": event, "deduplicated": not created}

    def unpack(self, event_id, box_id, bouquet_ids, at=None):
        """拆箱：花束从箱中取出。"""
        at = to_iso(self._at(at))
        with self.store.lock():
            event, created = self._append_event(event_id, {
                "type": "unpack", "at": at, "container_id": box_id, "bouquet_ids": list(bouquet_ids),
            })
            return {"event": event, "deduplicated": not created}

    def load(self, event_id, box_id, vehicle_id, at=None):
        """装箱上车。"""
        at = to_iso(self._at(at))
        with self.store.lock():
            for cid in (box_id, vehicle_id):
                if cid not in self.store.containers:
                    raise LineageError(f"载具不存在: {cid}")
            event, created = self._append_event(event_id, {
                "type": "load", "at": at, "container_id": box_id, "vehicle_id": vehicle_id,
            })
            return {"event": event, "deduplicated": not created}

    def unload(self, event_id, box_id, vehicle_id, at=None):
        """卸车。"""
        at = to_iso(self._at(at))
        with self.store.lock():
            event, created = self._append_event(event_id, {
                "type": "unload", "at": at, "container_id": box_id, "vehicle_id": vehicle_id,
            })
            return {"event": event, "deduplicated": not created}

    def transfer_vehicle(self, event_id, box_id, from_vehicle_id, to_vehicle_id, at=None):
        """换冷链车辆：原子地把箱从旧车转到新车，两辆车都留痕。"""
        at = to_iso(self._at(at))
        with self.store.lock():
            for cid in (box_id, from_vehicle_id, to_vehicle_id):
                if cid not in self.store.containers:
                    raise LineageError(f"载具不存在: {cid}")
            event, created = self._append_event(event_id, {
                "type": "transfer", "at": at, "container_id": box_id,
                "from_vehicle_id": from_vehicle_id, "to_vehicle_id": to_vehicle_id,
            })
            return {"event": event, "deduplicated": not created}

    def bind_logger(self, logger_id, container_id, at=None):
        """温度记录仪绑定到载具（换车后记录仪可继续跟随箱子）。"""
        at = to_iso(self._at(at))
        with self.store.lock():
            if container_id not in self.store.containers:
                raise LineageError(f"载具不存在: {container_id}")
            self.store.logger_bindings[logger_id] = {"container_id": container_id, "bound_at": at}
            return {"logger_id": logger_id, "container_id": container_id, "bound_at": at}

    # ---- 归属回放 -------------------------------------------------------

    def _events_sorted(self):
        return sorted(
            (self.store.lineage_events[eid] for eid in self.store.lineage_order),
            key=lambda e: (parse_iso(e["at"]), e["event_id"]),
        )

    def container_of_bouquet_at(self, bouquet_id, at):
        """返回某时刻花束所在的最内层容器（无则 None）。"""
        at = parse_iso(at)
        current = None
        for e in self._events_sorted():
            if parse_iso(e["at"]) > at:
                break
            if e["type"] in ("pack", "unpack") and bouquet_id in e["bouquet_ids"]:
                current = e["container_id"] if e["type"] == "pack" else None
        return current

    def vehicle_of_box_at(self, box_id, at):
        """返回某时刻箱所在车辆。"""
        at = parse_iso(at)
        current = None
        for e in self._events_sorted():
            if parse_iso(e["at"]) > at:
                break
            if e["type"] == "load" and e["container_id"] == box_id:
                current = e["vehicle_id"]
            elif e["type"] == "unload" and e["container_id"] == box_id:
                current = None
            elif e["type"] == "transfer" and e["container_id"] == box_id:
                current = e["to_vehicle_id"]
        return current

    def location_of_bouquet_at(self, bouquet_id, at):
        box = self.container_of_bouquet_at(bouquet_id, at)
        vehicle = self.vehicle_of_box_at(box, at) if box else None
        return {"box_id": box, "vehicle_id": vehicle}

    def bouquets_in_container_during(self, container_id, start, end):
        """圈定时间窗 [start, end] 内曾处于载具 container_id 内的花束。

        若载具是车辆，则找出窗内在该车辆上的箱及其花束；
        若载具是箱，则直接看花束在箱内的时间段是否与窗口有交叠。
        """
        start, end = parse_iso(start), parse_iso(end)
        container = self.store.containers.get(container_id)
        affected = set()
        events = self._events_sorted()

        if container and container.kind == "vehicle":
            # 逐箱求在车区间
            box_intervals = self._box_vehicle_intervals(events, container_id)
            for box_id, intervals in box_intervals.items():
                if _overlaps_any(intervals, start, end):
                    affected.update(self._bouquets_in_box_during(events, box_id, start, end))
        else:
            affected.update(self._bouquets_in_box_during(events, container_id, start, end))
        return sorted(affected)

    @staticmethod
    def _box_vehicle_intervals(events, vehicle_id):
        intervals = {}
        current_vehicle = {}  # box -> vehicle
        interval_start = {}
        for e in events:
            t = parse_iso(e["at"])
            if e["type"] == "load":
                current_vehicle[e["container_id"]] = e["vehicle_id"]
                if e["vehicle_id"] == vehicle_id:
                    interval_start[e["container_id"]] = t
            elif e["type"] == "unload":
                box = e["container_id"]
                if current_vehicle.get(box) == vehicle_id and box in interval_start:
                    intervals.setdefault(box, []).append((interval_start.pop(box), t))
                current_vehicle[box] = None
            elif e["type"] == "transfer":
                box = e["container_id"]
                if current_vehicle.get(box) == vehicle_id and box in interval_start:
                    intervals.setdefault(box, []).append((interval_start.pop(box), t))
                current_vehicle[box] = e["to_vehicle_id"]
                if e["to_vehicle_id"] == vehicle_id:
                    interval_start[box] = t
        for box, t0 in interval_start.items():
            intervals.setdefault(box, []).append((t0, None))
        return intervals

    @staticmethod
    def _bouquets_in_box_during(events, box_id, start, end):
        in_box = set()
        entered = {}
        found = set()
        for e in events:
            t = parse_iso(e["at"])
            if e["type"] == "pack" and e["container_id"] == box_id:
                for b in e["bouquet_ids"]:
                    in_box.add(b)
                    entered[b] = t
            elif e["type"] == "unpack" and e["container_id"] == box_id:
                for b in e["bouquet_ids"]:
                    if b in in_box and _overlap(entered.get(b), t, start, end):
                        found.add(b)
                    in_box.discard(b)
                    entered.pop(b, None)
        for b in list(in_box):
            if _overlap(entered.get(b), None, start, end):
                found.add(b)
        return found

    # ---- 来源档案 -------------------------------------------------------

    def provenance(self, bouquet_id):
        with self.store.lock():
            b = self.store.bouquets.get(bouquet_id)
            if not b:
                raise LineageError(f"花束不存在: {bouquet_id}")
            seed = self.store.seed_batches[b.seed_batch_id]
            house = self.store.greenhouses[b.house_id]
            shift = self.store.shifts[b.shift_id]
            ph = self.store.post_harvest[b.post_harvest_id]
            wait_minutes = (
                parse_iso(ph.precool_started_at) - parse_iso(shift.cut_at)
            ).total_seconds() / 60.0
            precool_minutes = (
                parse_iso(ph.precool_ended_at) - parse_iso(ph.precool_started_at)
            ).total_seconds() / 60.0
            return {
                "bouquet": b.to_dict(),
                "seed_batch": seed.to_dict(),
                "greenhouse": house.to_dict(),
                "harvest_shift": shift.to_dict(),
                "post_harvest": ph.to_dict(),
                "derived": {
                    "cultivar": seed.cultivar,
                    "maturity_stage": shift.maturity_stage,
                    "precool_wait_minutes": round(wait_minutes, 2),
                    "precool_minutes": round(precool_minutes, 2),
                },
            }

    def movement_timeline(self, bouquet_id):
        with self.store.lock():
            if bouquet_id not in self.store.bouquets:
                raise LineageError(f"花束不存在: {bouquet_id}")
            result = []
            for e in self._events_sorted():
                if e["type"] in ("pack", "unpack"):
                    if bouquet_id in e["bouquet_ids"]:
                        result.append(dict(e))
                elif e["type"] in ("load", "unload", "transfer"):
                    # 只在花束确实在箱内时，箱的车辆事件才算花束的经历
                    if self.container_of_bouquet_at(bouquet_id, parse_iso(e["at"])) == e["container_id"]:
                        result.append(dict(e))
            return result


def _overlap(interval_start, interval_end, win_start, win_end):
    if interval_start is None:
        return False
    interval_end = interval_end or win_end
    return interval_start <= win_end and interval_end >= win_start


def _overlaps_any(intervals, start, end):
    return any(_overlap(a, b, start, end) for a, b in intervals)
