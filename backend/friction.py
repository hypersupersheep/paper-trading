"""交易摩擦计算(回测与实盘共用)。

滑点支持三种模型:
  - "adaptive"(默认):平方根冲击模型 slippage = η·σ·√(下单额/日均成交额)·下单额。
    随【订单相对日成交额的参与率】和【波动率】自适应——单越大、票越波动,滑点越高。
    无成交量数据时退化为一个温和的固定 bps,避免给出离谱数值。
  - "bps":固定基点,slippage = 下单额 × value / 10000。
  - "fixed_tick":每股固定 value 元。

佣金双边收取;印花税按 A 股法规仅卖出单边(由调用方决定 side)。
"""

from __future__ import annotations

import math
from typing import Any

# 估算日均成交额(ADV)用的回溯窗口(交易日数)。
DEFAULT_ADV_WINDOW = 20
# 自适应滑点安全上限:防止极端参与率把滑点放大到离谱(300bps)。
MAX_SLIPPAGE_FRACTION = 0.03
# 没有成交量数据时的退化滑点(基点)。
NO_VOLUME_FALLBACK_BPS = 2.0
# σ(波动率)兜底:bar 缺高低价时按 1% 估。
SIGMA_FALLBACK = 0.01


def _bar_turnover(bar: dict[str, Any]) -> float:
    try:
        return float(bar.get("volume") or 0) * float(bar.get("close") or 0)
    except (TypeError, ValueError):
        return 0.0


def _bar_range_vol(bar: dict[str, Any]) -> float:
    """用单根 bar 的振幅 (high-low)/close 作为该期波动率 σ 的廉价代理。"""
    try:
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])
    except (KeyError, TypeError, ValueError):
        return 0.0
    if close <= 0:
        return 0.0
    return max(high - low, 0.0) / close


def adaptive_slippage_cost(
    quantity: float,
    fill_price: float,
    ref_bars: list[dict[str, Any]] | None,
    coefficient: float,
) -> float:
    """平方根冲击模型滑点(返回成交金额单位的成本)。

    ref_bars:该标的近段(日频)bar,用于估 ADV 与 σ;约定最后一根为参考波动 bar。
    coefficient(η):强度系数,即 account.slippage_value。
    """
    order_notional = abs(quantity) * fill_price
    if order_notional <= 0:
        return 0.0

    turnovers = [t for t in (_bar_turnover(b) for b in (ref_bars or [])) if t > 0]
    if not turnovers:
        # 没有成交量数据 → 退化为温和固定 bps,而不是乱估参与率。
        return round(order_notional * NO_VOLUME_FALLBACK_BPS / 10_000, 2)

    adv = sum(turnovers) / len(turnovers)
    participation = order_notional / adv if adv > 0 else 0.0
    sigma = _bar_range_vol(ref_bars[-1]) if ref_bars else 0.0
    if sigma <= 0:
        sigma = SIGMA_FALLBACK

    fraction = coefficient * sigma * math.sqrt(participation)
    fraction = min(max(fraction, 0.0), MAX_SLIPPAGE_FRACTION)
    return round(order_notional * fraction, 2)


def slippage_cost(
    model: str,
    *,
    quantity: float,
    fill_price: float,
    slippage_value: float,
    ref_bars: list[dict[str, Any]] | None = None,
) -> float:
    """按模型派发滑点计算。未知模型按 bps 处理。"""
    q = abs(quantity)
    if q <= 0 or fill_price <= 0:
        return 0.0
    if model == "fixed_tick":
        return round(q * slippage_value, 2)
    if model == "adaptive":
        return adaptive_slippage_cost(q, fill_price, ref_bars, coefficient=slippage_value)
    return round(q * fill_price * slippage_value / 10_000, 2)
