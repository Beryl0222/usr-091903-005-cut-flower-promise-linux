"""鲜切花保鲜承诺的运行入口。

保留稳定的服务身份与健康检查（/health），并挂载领域 API（/v1/...）：
溯源建档、版本化品质规则、接单承诺、温度采集、异常圈定补证、一次结算与三方视图。
"""

import argparse
from http.server import ThreadingHTTPServer

from freshkeep.clock import SimClock
from freshkeep.httpapi import ApiState, make_handler

SERVICE_ID = "cut-flower-promise"
SERVICE_NAME = "鲜切花保鲜承诺"

# 进程级共享状态（内存存储，便于本地联调；测试可自行构造 ApiState）
state = ApiState(clock=SimClock())
Handler = make_handler(state)


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 领域装配自检：规则版本化与冷量计算可正常工作
        from freshkeep.app import FreshKeepApp

        app = FreshKeepApp()
        app.seed_demo_rules()
        snapshot = app.rules.effective_at("2026-02-01T00:00:00Z")
        assert snapshot["version_id"] == "R-2026-01"
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
