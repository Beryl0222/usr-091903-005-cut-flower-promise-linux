"""版本化品质规则。

规则随时间演进（放宽或收紧），订单只能用"接单那一刻有效"的版本解释。
因此：
- 每条规则带 effective_from / effective_to；
- 接单时固化 rule_version 与规则快照内容；
- 旧订单永远不读取后来生效的新版本。
"""

from copy import deepcopy

from . import clock as timeutil
from .errors import NotFound, ValidationFailed

# 规则版本内容（内置基线；可通过命令追加新版本，仅追加不改写历史）。
# 字段含义：
#   vase_life_hours            各品种"采切时"承诺的基准瓶插期
#   maturity_factors           采切成熟度对瓶插期的系数
#   precool_penalty_per_min    采后预冷等待，每分钟扣减瓶插期（小时）
#   temp_budget_hours          各温度档每分钟消耗的"冷量分钟数"
#                              （理想冷藏 1:1；越热消耗越快）
#   temp_bands                 判定超温的温度档（℃）与承运责任口径
#   disconnect_temp_c          记录器断连区间采用的保守温度
#   max_arrival_hours          接单时可承诺的最长到货耗时
#   min_remaining_cool_min     承诺到货窗口要求的剩余冷量下限（分钟）
#   compensation               瓶插期缩水分档赔付比例（占订单金额）
#   quality_compensation       收货品相异常的定额赔付比例（按责任归属结算）
BUILTIN_VERSIONS = [
    {
        "version": "rule-v1-2026-08",
        "effective_from": "2026-08-01T00:00:00",
        "effective_to": "2026-09-10T00:00:00",
        "published_at": "2026-07-25T10:00:00",
        "note": "首发版本：卡罗拉/影星标准瓶插期与冷链冷量口径",
        "vase_life_hours": {"卡罗拉": 168, "影星": 144, "粉雪山": 120},
        "maturity_factors": {"tight_bud": 1.0, "commercial": 0.9, "open": 0.75},
        "precool_penalty_per_min_hours": 0.05,
        "quality_compensation": {"wilted": 0.30, "damaged": 0.50},
        "temp_budget": {
            # 档名: (温度下界℃, 温度上界℃, 每实分钟消耗的冷量分钟)
            "ideal": (0.0, 4.0, 1.0),
            "acceptable": (4.0, 8.0, 1.8),
            "warm": (8.0, 15.0, 3.0),
            "hot": (15.0, 99.0, 5.0),
        },
        "temp_bands": [
            {"name": "ideal", "min_c": 0.0, "max_c": 4.0, "excursion": False},
            {"name": "acceptable", "min_c": 4.0, "max_c": 8.0, "excursion": False},
            {"name": "warm", "min_c": 8.0, "max_c": 15.0, "excursion": True},
            {"name": "hot", "min_c": 15.0, "max_c": 99.0, "excursion": True},
        ],
        "disconnect_temp_c": 18.0,
        "min_connectivity_ratio": 0.9,
        "max_arrival_hours": 72,
        "min_remaining_cool_min": 600,
        "compensation": [
            # (缩水比例下限, 赔付比例)
            {"shortfall_min": 0.30, "pay_ratio": 0.30},
            {"shortfall_min": 0.20, "pay_ratio": 0.15},
            {"shortfall_min": 0.10, "pay_ratio": 0.05},
        ],
    },
    {
        "version": "rule-v2-2026-09",
        "effective_from": "2026-09-10T00:00:00",
        "effective_to": None,
        "published_at": "2026-09-05T09:00:00",
        "note": "v2：放宽成熟度系数与预冷惩罚，并上调最长到货耗时——不溯及旧订单",
        "vase_life_hours": {"卡罗拉": 168, "影星": 144, "粉雪山": 120},
        "maturity_factors": {"tight_bud": 1.05, "commercial": 0.95, "open": 0.85},
        "precool_penalty_per_min_hours": 0.03,
        "quality_compensation": {"wilted": 0.25, "damaged": 0.45},
        "temp_budget": {
            "ideal": (0.0, 4.0, 1.0),
            "acceptable": (4.0, 8.0, 1.5),
            "warm": (8.0, 15.0, 2.4),
            "hot": (15.0, 99.0, 4.0),
        },
        "temp_bands": [
            {"name": "ideal", "min_c": 0.0, "max_c": 4.0, "excursion": False},
            {"name": "acceptable", "min_c": 4.0, "max_c": 8.0, "excursion": False},
            {"name": "warm", "min_c": 8.0, "max_c": 15.0, "excursion": True},
            {"name": "hot", "min_c": 15.0, "max_c": 99.0, "excursion": True},
        ],
        "disconnect_temp_c": 12.0,
        "min_connectivity_ratio": 0.9,
        "max_arrival_hours": 96,
        "min_remaining_cool_min": 480,
        "compensation": [
            {"shortfall_min": 0.30, "pay_ratio": 0.25},
            {"shortfall_min": 0.20, "pay_ratio": 0.12},
            {"shortfall_min": 0.10, "pay_ratio": 0.04},
        ],
    },
]


class RuleBook:
    """规则内容的只读视图，支持按时间点取有效版本与按版本号取快照。"""

    def __init__(self, versions=None):
        self._versions = [deepfit(v) for v in (versions if versions is not None else BUILTIN_VERSIONS)]

    def all_versions(self):
        return [deepcopy(v) for v in self._versions]

    def normalize_windows(self):
        """按生效时间重排并关闭被后续版本接续的开放窗口。

        发布时已在内存关闭上一版；从事件流重建时没有关闭事件，
        这里用"窗口不得重叠"的不变量确定性地补回。
        """
        self._versions.sort(key=lambda v: v["effective_from"])
        for prev, nxt in zip(self._versions, self._versions[1:]):
            if prev["effective_to"] is None and nxt["effective_from"] >= prev["effective_from"]:
                prev["effective_to"] = nxt["effective_from"]

    def effective_at(self, at):
        """返回 at 时刻有效的规则版本（深拷贝快照）。"""
        for version in self._versions:
            if version["effective_from"] <= at and (
                version["effective_to"] is None or at < version["effective_to"]
            ):
                return deepcopy(version)
        raise NotFound(f"{at} 没有生效中的品质规则版本")

    def get(self, version):
        for candidate in self._versions:
            if candidate["version"] == version:
                return deepcopy(candidate)
        raise NotFound(f"规则版本不存在: {version}")

    def publish(self, version, effective_from, content, published_at=None):
        """登记新版本；与既有版本窗口重叠或版本号重复将被拒绝。"""
        if any(v["version"] == version for v in self._versions):
            raise ValidationFailed("规则版本已存在", version=version)
        if "vase_life_hours" not in content or "temp_budget" not in content:
            raise ValidationFailed("规则内容缺少必要字段")
        for v in self._versions:
            end = v["effective_to"] or effective_from
            if v["effective_from"] <= effective_from < end:
                raise ValidationFailed(
                    "新版本生效时间与既有版本窗口重叠",
                    version=v["version"], window=[v["effective_from"], v["effective_to"]],
                )
        record = deepfit(content)
        record.update(
            {
                "version": version,
                "effective_from": effective_from,
                "effective_to": None,
                "published_at": published_at or timeutil.utc_now_iso(),
            }
        )
        # 关闭上一个开放版本的窗口
        for v in self._versions:
            if v["effective_to"] is None:
                v["effective_to"] = effective_from
        self._versions.append(record)
        return deepcopy(record)


def deepfit(version):
    """规范化一份规则记录（补齐可空字段）。"""
    record = deepcopy(version)
    record.setdefault("effective_to", None)
    record.setdefault("published_at", record.get("effective_from"))
    record.setdefault("note", "")
    return record


def band_for_temp(rule, temp_c):
    for band in rule["temp_bands"]:
        if band["min_c"] <= temp_c < band["max_c"]:
            return band
    # 温度上界用闭区间兜底
    return rule["temp_bands"][-1]


def budget_rate(rule, temp_c):
    """该温度下每实分钟消耗的冷量分钟数。"""
    for _name, (lo, hi, rate) in rule["temp_budget"].items():
        if lo <= temp_c < hi:
            return rate
    return list(rule["temp_budget"].values())[-1][2]


def compensation_ratio(rule, shortfall_ratio):
    """按瓶插期缩水比例取赔付档；不足最低档返回 0。"""
    ratio = 0.0
    for tier in rule["compensation"]:
        if shortfall_ratio >= tier["shortfall_min"]:
            ratio = max(ratio, tier["pay_ratio"])
    return ratio
