"""品质规则版本化。

规则是一份带生效时间的文档：某时刻有效的规则 = 生效时间不晚于该时刻的最新版本。
接单时把命中的规则**全文快照**进订单，此后履约、异常判定、赔付计算都只认快照——
后来放宽（或收紧）的标准绝不用于解释旧订单。
"""

import copy

from .clock import now_iso, parse_iso, to_iso

DEFAULT_PARAMS = {
    # 不同采切成熟度（1 最生，5 最熟）在理想冷链下的基础瓶插天数
    "base_vase_days_by_maturity": {"1": 10.0, "2": 9.0, "3": 7.5, "4": 5.5, "5": 3.5},
    "q10": 2.5,                 # 温度每升高 10℃，衰老速率倍数
    "reference_temp_c": 20.0,   # 瓶插天数标定温度
    "ideal_chain_temp_c": 2.0,  # 理想冷链温度
    "ambient_temp_c": 22.0,     # 无温度记录时采后等待段的保守温度
    # 采切后未及时预冷，每等待 1 小时扣减的瓶插天数（计入种植端责任）
    "precool_wait_penalty_per_hour": 0.08,
    "precool_wait_warning_hours": 1.0,
    # 各销售渠道要求的到货后最低瓶插天数与运输时限
    "min_vase_days_by_channel": {
        "wholesale_market": 4.0,
        "wedding": 5.0,
        "florist": 4.0,
    },
    "max_transit_hours_by_channel": {
        "wholesale_market": 96,
        "wedding": 72,
        "florist": 84,
    },
    "delay_tolerance_minutes_by_channel": {
        "wholesale_market": 120,
        "wedding": 60,
        "florist": 90,
    },
    # 超温等级判定
    "excursion": {
        "notice_above_c": 6.0,    # 连续超过该温度即关注
        "minor_above_c": 8.0,
        "major_above_c": 12.0,
        "minor_minutes": 30,      # 超温持续达到该分钟数构成一次事故
        "gap_tolerance_minutes": 15,  # 记录仪断连超过该时长需补传说明
    },
    # 赔付比例（按承诺缩水/事故等级），以花束成交价为基数
    "compensation": {
        "vase_shortfall_bands": [  # 实际瓶插低于承诺的比例 -> 赔付比例
            {"shortfall_ratio_gte": 0.50, "refund_ratio": 1.00},
            {"shortfall_ratio_gte": 0.30, "refund_ratio": 0.60},
            {"shortfall_ratio_gte": 0.15, "refund_ratio": 0.30},
        ],
        "minor_excursion_refund_ratio": 0.15,
        "major_excursion_refund_ratio": 0.50,
        "delay_bands": [  # 超出容忍分钟数 -> 赔付比例
            {"late_minutes_gte": 360, "refund_ratio": 0.40},
            {"late_minutes_gte": 120, "refund_ratio": 0.20},
        ],
        "grower_precool_deduction_per_hour": 0.02,  # 超时预冷等待从种植款扣（比例/小时）
        "grower_precool_deduction_cap": 0.30,
    },
}


class RuleError(ValueError):
    pass


class RuleBook:
    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock

    def add_version(self, version_id, effective_from, cultivar_overrides=None,
                    params_overrides=None, note=None):
        """发布新版本。参数覆盖与品种覆盖都做深拷贝隔离。"""
        effective_from = to_iso(effective_from)
        with self.store.lock():
            if version_id in self.store.rule_versions:
                raise RuleError(f"规则版本已存在: {version_id}")
            params = copy.deepcopy(DEFAULT_PARAMS)
            if params_overrides:
                _deep_merge(params, copy.deepcopy(params_overrides))
            version = {
                "version_id": version_id,
                "effective_from": effective_from,
                "note": note,
                "published_at": self.clock.now_iso() if self.clock else now_iso(),
                "params": params,
                "cultivar_overrides": copy.deepcopy(cultivar_overrides or {}),
            }
            self.store.rule_versions[version_id] = version
            self.store.rule_order.append(version_id)
            return self.public_version(version_id)

    def effective_at(self, at):
        """返回 at 时刻有效版本的完整快照（深拷贝，调用方可以存进订单）。"""
        at = parse_iso(at)
        with self.store.lock():
            candidates = [
                self.store.rule_versions[vid]
                for vid in self.store.rule_order
                if parse_iso(self.store.rule_versions[vid]["effective_from"]) <= at
            ]
            if not candidates:
                raise RuleError(f"在 {to_iso(at)} 之前没有任何生效的品质规则，无法接单")
            chosen = max(candidates, key=lambda v: parse_iso(v["effective_from"]))
            return copy.deepcopy(chosen)

    def get_version(self, version_id):
        with self.store.lock():
            if version_id not in self.store.rule_versions:
                raise RuleError(f"规则版本不存在: {version_id}")
            return copy.deepcopy(self.store.rule_versions[version_id])

    def public_version(self, version_id):
        v = self.get_version(version_id)
        return {
            "version_id": v["version_id"],
            "effective_from": v["effective_from"],
            "published_at": v["published_at"],
            "note": v["note"],
            "params": v["params"],
            "cultivar_overrides": v["cultivar_overrides"],
        }

    def list_versions(self):
        with self.store.lock():
            return [self.public_version(vid) for vid in self.store.rule_order]

    @staticmethod
    def resolve_params(version_snapshot, cultivar):
        """把默认参数与品种覆盖合并成该品种的最终参数（订单每行解析后入快照）。"""
        params = copy.deepcopy(version_snapshot["params"])
        override = version_snapshot.get("cultivar_overrides", {}).get(cultivar)
        if override:
            _deep_merge(params, copy.deepcopy(override))
        return params


def _deep_merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base
