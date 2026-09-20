"""JSON HTTP 适配层：把领域服务暴露为稳定的 REST 接口。"""

import json
import re
from http.server import BaseHTTPRequestHandler

from .clock import SimClock
from .app import FreshKeepApp


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# (方法, 正则, 处理函数名)
ROUTES = [
    ("GET", r"^/health$", "health"),
    ("POST", r"^/v1/admin/reset$", "reset"),

    ("POST", r"^/v1/rules/versions$", "rule_publish"),
    ("GET", r"^/v1/rules/versions$", "rule_list"),
    ("GET", r"^/v1/rules/versions/(?P<vid>[^/]+)$", "rule_get"),

    ("POST", r"^/v1/lineage/seed-batches$", "seed_batch_create"),
    ("POST", r"^/v1/lineage/greenhouses$", "greenhouse_create"),
    ("POST", r"^/v1/lineage/shifts$", "shift_create"),
    ("POST", r"^/v1/lineage/post-harvest$", "post_harvest_create"),
    ("POST", r"^/v1/lineage/bouquets$", "bouquet_create"),
    ("POST", r"^/v1/lineage/containers$", "container_create"),
    ("POST", r"^/v1/lineage/events$", "lineage_event"),
    ("POST", r"^/v1/lineage/loggers$", "logger_bind"),
    ("GET", r"^/v1/bouquets/(?P<bid>[^/]+)/provenance$", "bouquet_provenance"),

    ("POST", r"^/v1/temperature/readings$", "temp_reading"),
    ("POST", r"^/v1/temperature/gaps$", "temp_gap"),
    ("POST", r"^/v1/temperature/gaps/backfill$", "temp_backfill"),
    ("GET", r"^/v1/temperature/loggers/(?P<logger>[^/]+)/timeline$", "temp_timeline"),

    ("POST", r"^/v1/orders$", "order_place"),
    ("GET", r"^/v1/orders/(?P<oid>[^/]+)$", "order_get"),
    ("POST", r"^/v1/orders/(?P<oid>[^/]+)/carrier-events$", "carrier_event"),
    ("POST", r"^/v1/orders/(?P<oid>[^/]+)/receipts$", "receipt_sign"),
    ("POST", r"^/v1/orders/(?P<oid>[^/]+)/scan$", "order_scan"),
    ("POST", r"^/v1/scan$", "scan_all"),

    ("GET", r"^/v1/incidents$", "incident_list"),
    ("GET", r"^/v1/incidents/(?P<iid>[^/]+)$", "incident_get"),
    ("POST", r"^/v1/incidents/(?P<iid>[^/]+)/evidence$", "evidence_submit"),
    ("POST", r"^/v1/incidents/(?P<iid>[^/]+)/adjudicate$", "incident_adjudicate"),

    ("GET", r"^/v1/claims$", "claim_list"),
    ("POST", r"^/v1/orders/(?P<oid>[^/]+)/lines/(?P<lid>[^/]+)/settle$", "line_settle"),
    ("GET", r"^/v1/settlements/(?P<sid>[^/]+)$", "settlement_get"),

    ("GET", r"^/v1/views/customer-service/(?P<oid>[^/]+)$", "view_cs"),
    ("GET", r"^/v1/views/grower/(?P<gid>[^/]+)$", "view_grower"),
    ("GET", r"^/v1/views/replay/(?P<oid>[^/]+)$", "view_replay"),
]

COMPILED = [(method, re.compile(pattern), handler) for method, pattern, handler in ROUTES]


class ApiState:
    def __init__(self, clock=None):
        self.clock = clock or SimClock()
        self.reset()

    def reset(self):
        self.app = FreshKeepApp(clock=self.clock)


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ApiError(400, f"请求体不是合法 JSON: {exc}")
            if not isinstance(data, dict):
                raise ApiError(400, "请求体必须是 JSON 对象")
            return data

        def _dispatch(self, method):
            path = self.path.split("?", 1)[0]
            match = None
            handler_name = None
            for m, pattern, name in COMPILED:
                if m == method:
                    match = pattern.match(path)
                    if match:
                        handler_name = name
                        break
            if not match:
                self._send(404, {"error": "not_found", "path": path})
                return
            try:
                body = self._read_body() if method in ("POST", "PUT", "PATCH") else {}
                result = getattr(Router(state), handler_name)(match.groupdict(), body)
                self._send(200, result)
            except ApiError as exc:
                self._send(exc.status, {"error": exc.message})
            except (KeyError, ValueError) as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - 兜底，不泄露栈到连接外
                self._send(500, {"error": f"内部错误: {exc}"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, *_args):
            return

    return Handler


class Router:
    def __init__(self, state):
        self.state = state

    @property
    def app(self):
        return self.state.app

    # 基础 ----------------------------------------------------------------

    def health(self, _params, _body):
        from service import health_payload

        return health_payload()

    def reset(self, _params, _body):
        self.state.reset()
        return {"status": "reset"}

    # 规则 ----------------------------------------------------------------

    def rule_publish(self, _p, body):
        return self.app.rules.add_version(
            body["version_id"], body["effective_from"],
            cultivar_overrides=body.get("cultivar_overrides"),
            params_overrides=body.get("params_overrides"),
            note=body.get("note"),
        )

    def rule_list(self, _p, _b):
        return {"versions": self.app.rules.list_versions()}

    def rule_get(self, params, _b):
        return self.app.rules.public_version(params["vid"])

    # 溯源 ----------------------------------------------------------------

    def seed_batch_create(self, _p, body):
        return self.app.lineage.register_seed_batch(
            body["batch_id"], body["cultivar"],
            supplier=body.get("supplier"), planted_at=body.get("planted_at"),
            extra=body.get("extra"))

    def greenhouse_create(self, _p, body):
        return self.app.lineage.register_greenhouse(
            body["house_id"], name=body.get("name"),
            location=body.get("location"), grower_id=body.get("grower_id"))

    def shift_create(self, _p, body):
        return self.app.lineage.register_shift(
            body["shift_id"], body["house_id"], body["cut_at"],
            body["maturity_stage"], supervisor=body.get("supervisor"))

    def post_harvest_create(self, _p, body):
        return self.app.lineage.register_post_harvest(
            body["record_id"], body["shift_id"],
            body["precool_started_at"], body["precool_ended_at"],
            preservative=body.get("preservative"),
            handled_by=body.get("handled_by"), notes=body.get("notes"))

    def bouquet_create(self, _p, body):
        return self.app.lineage.register_bouquet(
            body["bouquet_id"], body["seed_batch_id"], body["house_id"],
            body["shift_id"], body["post_harvest_id"],
            stems=body.get("stems", 1), created_at=body.get("created_at"))

    def container_create(self, _p, body):
        return self.app.lineage.register_container(
            body["container_id"], body["kind"], capacity=body.get("capacity"))

    def lineage_event(self, _p, body):
        kind = body["type"]
        at = body.get("at")
        lin = self.app.lineage
        if kind == "pack":
            return lin.pack(body["event_id"], body["container_id"],
                            body["bouquet_ids"], at)
        if kind == "unpack":
            return lin.unpack(body["event_id"], body["container_id"],
                              body["bouquet_ids"], at)
        if kind == "load":
            return lin.load(body["event_id"], body["container_id"],
                            body["vehicle_id"], at)
        if kind == "unload":
            return lin.unload(body["event_id"], body["container_id"],
                              body["vehicle_id"], at)
        if kind == "transfer":
            return lin.transfer_vehicle(
                body["event_id"], body["container_id"],
                body["from_vehicle_id"], body["to_vehicle_id"], at)
        raise ApiError(400, f"未知物流事件类型: {kind}")

    def logger_bind(self, _p, body):
        return self.app.lineage.bind_logger(
            body["logger_id"], body["container_id"], body.get("at"))

    def bouquet_provenance(self, params, _b):
        return self.app.lineage.provenance(params["bid"])

    # 温度 ----------------------------------------------------------------

    def temp_reading(self, _p, body):
        result = self.app.monitoring.record_reading(
            body["logger_id"], body["seq"], body["read_at"], body["temp_c"],
            event_id=body.get("event_id"))
        if not result["deduplicated"]:
            result["new_incidents"] = self.app.fulfillment.scan_all_orders()
        return result

    def temp_gap(self, _p, body):
        result = self.app.monitoring.report_gap(
            body["logger_id"], body["gap_id"], body["disconnected_at"],
            resumed_at=body.get("resumed_at"), reason=body.get("reason"))
        if not result["deduplicated"]:
            result["new_incidents"] = self.app.fulfillment.scan_all_orders()
        return result

    def temp_backfill(self, _p, body):
        result = self.app.monitoring.backfill_readings(
            body["logger_id"], body["readings"],
            gap_id=body.get("gap_id"), event_id=body.get("event_id"))
        if result.get("inserted") or body.get("gap_id"):
            result["new_incidents"] = self.app.fulfillment.scan_all_orders()
        return result

    def temp_timeline(self, params, _b):
        return {"readings": self.app.monitoring.timeline(params["logger"])}

    # 订单与履约 -----------------------------------------------------------

    def order_place(self, _p, body):
        return self.app.ordering.place_order(
            body["order_id"], body["lines"],
            channel_default=body.get("channel_default"),
            placed_at=body.get("placed_at"),
            customer=body.get("customer"), note=body.get("note"))

    def order_get(self, params, _b):
        return self.app.ordering.public_order(params["oid"])

    def carrier_event(self, params, body):
        return self.app.fulfillment.carrier_event(
            body["event_id"], params["oid"], body["event_type"], body["at"],
            vehicle_id=body.get("vehicle_id"), line_id=body.get("line_id"),
            payload=body.get("payload"))

    def receipt_sign(self, params, body):
        return self.app.fulfillment.sign_receipt(
            body["receipt_id"], params["oid"], body["line_id"],
            body["bouquet_ids"], body["at"],
            condition_note=body.get("condition_note"),
            receiver=body.get("receiver"))

    def order_scan(self, params, _b):
        return {"incidents": self.app.fulfillment.scan_order(params["oid"])}

    def scan_all(self, _p, _b):
        return {"incidents": self.app.fulfillment.scan_all_orders()}

    # 事故与理赔 -----------------------------------------------------------

    def incident_list(self, _p, _b):
        return {"incidents": self.app.fulfillment.list_incidents()}

    def incident_get(self, params, _b):
        return self.app.fulfillment.public_incident(params["iid"])

    def evidence_submit(self, params, body):
        return self.app.fulfillment.submit_evidence(
            params["iid"], body["party"], body["explanation"],
            evidence_ref=body.get("evidence_ref"),
            documents=body.get("documents"), at=body.get("at"))

    def incident_adjudicate(self, params, body):
        return self.app.fulfillment.adjudicate(
            params["iid"], body["liability"], note=body.get("note"),
            outcome=body.get("outcome"), actor=body.get("actor", "manager"),
            at=body.get("at"))

    def claim_list(self, _p, _b):
        return {"claims": self.app.fulfillment.list_claims()}

    def line_settle(self, params, body):
        return self.app.fulfillment.settle_line(
            params["oid"], params["lid"], at=body.get("at"))

    def settlement_get(self, params, _b):
        return self.app.fulfillment.public_settlement(params["sid"])

    # 视图 ----------------------------------------------------------------

    def view_cs(self, params, _b):
        return self.app.views.customer_service_view(params["oid"])

    def view_grower(self, params, _b):
        return self.app.views.grower_view(params["gid"])

    def view_replay(self, params, _b):
        return self.app.views.replay_order(params["oid"])
