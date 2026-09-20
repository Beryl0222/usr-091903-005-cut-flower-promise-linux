"""HTTP JSON API 适配层（仅依赖标准库）。"""

import re

from . import views
from .app import FreshnessService
from .errors import FreshnessError
from .rules import RuleBook
from .store import EventStore


def build_service(data_path=None):
    return FreshnessService(store=EventStore(data_path), rulebook=RuleBook())


class APIHandler:
    """把 JSON 请求路由到 FreshnessService；由根 service.py 挂载在 /api 下。"""

    def __init__(self, service):
        self.service = service

    # ── 分派入口 ─────────────────────────────────────────────
    def handle(self, method, path, query, body):
        """返回 (status, dict)。path 已去掉 /api 前缀。"""
        routes = self._routes()
        for pattern, methods, handler in routes:
            match = pattern.fullmatch(path)
            if match:
                if method not in methods:
                    return 405, {"error": "method_not_allowed"}
                with self.service.store.lock:
                    return handler(query, body, **match.groupdict())
        return 404, {"error": "not_found", "path": path}

    def _routes(self):
        S = self.service
        p_post = "POST"
        g = "GET"
        return [
            (re.compile(r"/rules$"), {g}, lambda q, b: (200, {"versions": S.rulebook.all_versions()})),
            (re.compile(r"/rules/publish$"), {p_post},
             lambda q, b: (200, S.publish_rule(b["version"], b["effective_from"], b["content"],
                                               note=b.get("note", ""), at=b.get("at")))),

            (re.compile(r"/seed-batches$"), {p_post},
             lambda q, b: (200, {"seed_batch_id": S.register_seed_batch(
                 b["seed_batch_id"], b["cultivar"], b.get("supplier"),
                 b.get("planted_at"), at=b.get("at"))})),
            (re.compile(r"/greenhouses$"), {p_post},
             lambda q, b: (200, {"greenhouse_id": S.register_greenhouse(
                 b["greenhouse_id"], b.get("name"), at=b.get("at"))})),
            (re.compile(r"/cut-shifts$"), {p_post},
             lambda q, b: (200, {"shift_id": S.perform_cut_shift(
                 b["shift_id"], b["greenhouse_id"], b["seed_batch_id"],
                 b.get("shift_code"), at=b.get("at"))})),
            (re.compile(r"/bouquets/harvest$"), {p_post},
             lambda q, b: (200, {"bouquet_ids": S.harvest_bouquets(
                 b["shift_id"], b["bouquet_ids"], b.get("maturity", "commercial"),
                 b.get("stem_count", 20), at=b.get("at"))})),
            (re.compile(r"/postharvest$"), {p_post},
             lambda q, b: (200, {"record_id": S.record_postharvest(
                 b["bouquet_ids"], b.get("precool_wait_min", 0), b.get("pulse_solution"),
                 b.get("grading"), b.get("note", ""), b.get("record_id"), at=b.get("at"))})),

            (re.compile(r"/loggers/bind$"), {p_post},
             lambda q, b: (200, {"logger_id": S.bind_logger(
                 b["logger_id"], b["target_type"], b["target_id"], at=b.get("at"))})),
            (re.compile(r"/boxes/pack$"), {p_post},
             lambda q, b: (200, {"box_id": S.pack_box(
                 b["box_id"], b["bouquet_ids"], b.get("logger_id"), at=b.get("at"))})),
            (re.compile(r"/boxes/split$"), {p_post},
             lambda q, b: (200, {"box_ids": S.split_box(b["from_box_id"], b["splits"], at=b.get("at"))})),

            (re.compile(r"/shipments$"), {p_post},
             lambda q, b: (200, {"shipment_id": S.create_shipment(
                 b["shipment_id"], b["carrier"], b["box_ids"], b["vehicle_id"],
                 b.get("vehicle_logger_id"), b.get("route", ""), at=b.get("at"))})),
            (re.compile(r"/shipments/change-vehicle$"), {p_post},
             lambda q, b: (200, {"vehicle_id": S.change_vehicle(
                 b["shipment_id"], b["to_vehicle_id"],
                 b.get("to_vehicle_logger_id"), at=b.get("at"))})),
            (re.compile(r"/shipments/arrive$"), {p_post},
             lambda q, b: (200, {"arrived_at": S.shipment_arrived(
                 b["shipment_id"], b.get("arrived_at"), at=b.get("at"))})),

            (re.compile(r"/temperatures$"), {p_post},
             lambda q, b: (200, S.record_temperature(
                 b["logger_id"], b["at"], b["temp_c"],
                 b.get("received_at"), at=b.get("recorded_at")))),
            (re.compile(r"/temperatures/batch$"), {p_post},
             lambda q, b: (200, S.record_temperature_batch(b["readings"], at=b.get("at")))),

            (re.compile(r"/orders/quote$"), {p_post},
             lambda q, b: (200, S.quote_order(b["bouquet_ids"], at=b.get("at")))),
            (re.compile(r"/orders$"), {p_post},
             lambda q, b: (200, S.accept_order(
                 b["order_id"], b["bouquet_ids"], b["amount"], b.get("channel", "wholesale"),
                 b.get("customer", ""), b.get("destination", ""),
                 b.get("idempotency_key"), at=b.get("at")))),
            (re.compile(r"/orders/(?P<order_id>[^/]+)$"), {g},
             lambda q, b, order_id: (200, S.get_order(order_id))),
            (re.compile(r"/orders/(?P<order_id>[^/]+)/replay$"), {g},
             lambda q, b, order_id: (200, S.replay(order_id))),
            (re.compile(r"/orders/(?P<order_id>[^/]+)/settle$"), {p_post},
             lambda q, b, order_id: (200, S.settle_order(order_id, at=b.get("at")))),
            (re.compile(r"/orders/(?P<order_id>[^/]+)/settlement$"), {g},
             lambda q, b, order_id: (200, S.get_settlement(order_id))),

            (re.compile(r"/carrier-callbacks$"), {p_post},
             lambda q, b: (200, S.carrier_callback(
                 b["shipment_id"], b["status"], b["occurred_at"],
                 b["idempotency_key"], at=b.get("at"), note=b.get("note", "")))),
            (re.compile(r"/receipts$"), {p_post},
             lambda q, b: (200, {"receipt_id": S.confirm_receipt(
                 b["order_id"], b["bouquet_ids"], b["received_at"],
                 b.get("condition", "normal"), b.get("shipment_id"),
                 b.get("note", ""), b.get("receipt_id"), at=b.get("at"))})),

            (re.compile(r"/flags$"), {g},
             lambda q, b: (200, {"flags": S.list_flags(
                 q.get("order_id", [None])[0], q.get("status", [None])[0])})),
            (re.compile(r"/flags/manual$"), {p_post},
             lambda q, b: (200, {"flag_id": S.open_flag_manual(
                 b["order_id"], b["reason"], b["bouquet_ids"],
                 b.get("detail", ""), at=b.get("at"))})),
            (re.compile(r"/flags/(?P<flag_id>[^/]+)/evidence$"), {p_post},
             lambda q, b, flag_id: (200, {"evidence_id": S.submit_evidence(
                 flag_id, b["party"], b.get("kind", "note"), b.get("note", ""),
                 b.get("evidence_id"), at=b.get("at"))})),
            (re.compile(r"/flags/(?P<flag_id>[^/]+)/rule$"), {p_post},
             lambda q, b, flag_id: (200, S.rule_flag(
                 flag_id, b["liable_party"], b["resolution"],
                 b.get("reason", ""), at=b.get("at")))),

            (re.compile(r"/bouquets/(?P<bouquet_id>[^/]+)$"), {g},
             lambda q, b, bouquet_id: (200, S.get_bouquet(bouquet_id))),

            (re.compile(r"/views/customer-service/orders/(?P<order_id>[^/]+)$"), {g},
             lambda q, b, order_id: (200, views.customer_service_view(S, order_id))),
            (re.compile(r"/views/grower$"), {g},
             lambda q, b: (200, views.grower_view(
                 S, q.get("greenhouse_id", [None])[0], q.get("seed_batch_id", [None])[0]))),
            (re.compile(r"/views/management$"), {g},
             lambda q, b: (200, views.management_view(S))),
        ]


def dispatch(api, method, path, query, body):
    """带统一错误转换的分派。"""
    try:
        return api.handle(method, path, query, body)
    except FreshnessError as error:
        return error.http_status, error.to_dict()
    except KeyError as error:
        return 422, {"error": "missing_field", "field": error.args[0]}
    except (ValueError, TypeError) as error:
        return 400, {"error": "bad_request", "message": str(error)}
