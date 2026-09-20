"""领域错误。"""


class FreshnessError(Exception):
    """所有可预期业务错误的基类，code 用于稳定的 API 响应。"""

    code = "domain_error"
    http_status = 400

    def __init__(self, message, **context):
        super().__init__(message)
        self.message = message
        self.context = context

    def to_dict(self):
        data = {"error": self.code, "message": self.message}
        if self.context:
            data["context"] = self.context
        return data


class NotFound(FreshnessError):
    code = "not_found"
    http_status = 404


class Conflict(FreshnessError):
    code = "conflict"
    http_status = 409


class ValidationFailed(FreshnessError):
    code = "validation_failed"
    http_status = 422


class RuleError(FreshnessError):
    """无法给出承诺等规则判定失败。"""

    code = "rule_rejected"
    http_status = 422


class SettlementClosed(FreshnessError):
    code = "settlement_closed"
    http_status = 409
