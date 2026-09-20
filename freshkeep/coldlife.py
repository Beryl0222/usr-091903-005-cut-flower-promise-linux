"""剩余冷量 / 瓶插寿命模型（纯函数，便于测试与回放）。

采用采后生理学常用的 Q10 温度积算：
    衰老速率 r(T) = Q10 ** ((T - 参考温度) / 10)
花束的“寿命预算”为参考温度下的当量小时数（基础瓶插天数 × 24），
每一段经历按当时温度消耗 r(T) × 时长。剩余当量小时在客户瓶中（参考温度）
折算回瓶插天数。

同一份函数既用于接单时的理想预测（未发生的运输段按理想冷链温度），
也用于到货后的按实测温度复盘——决定因此可解释、可复核。
"""


def aging_rate(temp_c, q10, reference_temp_c):
    return q10 ** ((temp_c - reference_temp_c) / 10.0)


def consumed_hours(segments, q10, reference_temp_c):
    """segments: [(temp_c, hours), ...]，返回参考温度当量消耗小时。"""
    total = 0.0
    for temp_c, hours in segments:
        total += aging_rate(temp_c, q10, reference_temp_c) * hours
    return total


def budget_hours(base_vase_days):
    return base_vase_days * 24.0


def remaining_vase_days(total_budget_hours, consumed_hours_value):
    remaining = total_budget_hours - consumed_hours_value
    return max(0.0, remaining) / 24.0


def floor_half(value):
    """对外承诺向下取整到 0.5 天，只保守、不冒进。"""
    import math

    return max(0.0, math.floor(value * 2) / 2.0)


def evaluate(params, cultivar_params, maturity_stage, past_segments, planned_segments):
    """接单评估：返回预算、已消耗、运输预计消耗、到货剩余瓶插天数。

    - past_segments: 接单前已发生的经历（预冷等待、预冷存储等）
    - planned_segments: 按理想冷链预估的运输经历
    """
    q10 = params["q10"]
    ref = params["reference_temp_c"]
    base_table = cultivar_params.get("base_vase_days_by_maturity") or params["base_vase_days_by_maturity"]
    key = str(maturity_stage)
    if key not in base_table:
        raise ValueError(f"未知的采切成熟度等级: {maturity_stage}")
    base_days = base_table[key]

    budget = budget_hours(base_days)
    past_consumed = consumed_hours(past_segments, q10, ref)
    planned_consumed = consumed_hours(planned_segments, q10, ref)
    vase_at_arrival = remaining_vase_days(budget, past_consumed + planned_consumed)
    return {
        "base_vase_days": base_days,
        "budget_hours": round(budget, 3),
        "past_consumed_hours": round(past_consumed, 3),
        "planned_transit_consumed_hours": round(planned_consumed, 3),
        "remaining_vase_days_at_arrival": round(vase_at_arrival, 3),
    }


def max_transit_hours(params, remaining_budget_hours, min_vase_days):
    """在保证到货后最低瓶插天数的前提下，理想冷链最多还能走多少小时。"""
    q10 = params["q10"]
    ref = params["reference_temp_c"]
    rate = aging_rate(params["ideal_chain_temp_c"], q10, ref)
    available = remaining_budget_hours - min_vase_days * 24.0
    if available <= 0:
        return 0.0
    return available / rate
