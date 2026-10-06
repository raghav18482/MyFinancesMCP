"""The feature contract: an explicit, ordered, versioned list of model inputs.

Why this file exists. The previous pipeline trained on five-minute bars and
served daily bars using the same 35 feature *names*, so nothing ever errored —
the model simply read a different language at inference time and produced
confident nonsense. Names alone are not a contract.

So the contract carries three things a model is pinned to, all recorded in the
registry at training time and checked before every prediction:

  * ``FEATURE_SET_VERSION`` — bump whenever the list or any definition changes
  * the ordered feature list — order matters, the model takes a bare array
  * ``interval``            — stored per model, not here; enforced by the caller

Bump the version for ANY semantic change, including redefining an existing
feature. A stale model then refuses to run instead of silently misreading.
"""
from __future__ import annotations

# ── Version ────────────────────────────────────────────────────────────────
# 1 : initial honest contract. Daily bars only. Technical + cross-sectional +
#     market context + flow + fundamental + sentiment blocks.
FEATURE_SET_VERSION = 1

# Only daily bars are supported. The intraday horizons were removed because
# they are not tractable from candle data (see the design report, Figure 10).
SUPPORTED_INTERVALS = ("ONE_DAY",)


# ── Blocks ─────────────────────────────────────────────────────────────────
# Each block is built by one module in this package. Keeping the lists here
# rather than in the builders means the contract can be read in one place.

TECHNICAL = [
    "rsi_14", "rsi_slope_5",
    "macd", "macd_signal", "macd_histogram", "macd_slope_5",
    "sma_20_dist", "sma_50_dist", "sma_200_dist",
    "ema_9_dist", "ema_21_dist",
    "bb_position", "bb_width",
    "adx_14",
    "stoch_k", "stoch_d",
    "atr_14_pct",
    "volume_ratio", "volume_trend",
    "return_1", "return_3", "return_5", "return_10", "return_20",
    "volatility_5", "volatility_10", "volatility_20",
    "candle_body_ratio", "upper_shadow", "lower_shadow", "high_low_range",
    "dow_sin", "dow_cos",
]

# Percentile rank of a subset of the technical features, computed per date
# across the universe and within the stock's NSE industry. This is what lets
# the model learn relative strength instead of market direction.
CROSS_SECTIONAL_BASE = [
    "rsi_14", "return_5", "return_20", "volatility_20",
    "sma_50_dist", "macd_histogram", "atr_14_pct",
]
CROSS_SECTIONAL = (
    [f"xs_uni_{f}" for f in CROSS_SECTIONAL_BASE]
    + [f"xs_ind_{f}" for f in CROSS_SECTIONAL_BASE]
)

MARKET_CONTEXT = [
    "mkt_return_1", "mkt_return_5", "mkt_return_20",
    "mkt_volatility_20",
    "mkt_breadth_adv_pct",
    "rel_strength_5", "rel_strength_20",
    "ind_return_5", "ind_return_20",
    "rel_to_industry_20",
]

# Delivery percentage and turnover come free with the NSE bhavcopy and are
# absent from Angel's candle API entirely.
FLOW = [
    "deliv_pct", "deliv_pct_z20", "deliv_pct_chg_5",
    "turnover_log", "turnover_rank_uni",
    "avg_trade_size_z20",
]

# Read through the point-in-time store, never from a live fetch.
FUNDAMENTAL = [
    "fund_score_total", "fund_coverage",
    "fund_pillar_valuation", "fund_pillar_profitability",
    "fund_pillar_balance_sheet", "fund_pillar_growth",
    "fund_pe_ratio", "fund_pb_ratio", "fund_roe", "fund_debt_to_equity",
    "fund_revenue_growth", "fund_earnings_growth",
    "fund_is_stale_days",
]

SENTIMENT = [
    "sent_net_1d", "sent_net_3d", "sent_net_7d",
    "sent_article_count_7d", "sent_has_news",
]

BLOCKS: dict[str, list[str]] = {
    "technical": TECHNICAL,
    "cross_sectional": CROSS_SECTIONAL,
    "market_context": MARKET_CONTEXT,
    "flow": FLOW,
    "fundamental": FUNDAMENTAL,
    "sentiment": SENTIMENT,
}

FEATURE_NAMES: list[str] = [name for block in BLOCKS.values() for name in block]

# Blocks that may legitimately be entirely null for historical rows, because
# the data only accumulates forward. A model trained without them records that
# fact in its registry entry rather than pretending they were present.
FORWARD_ONLY_BLOCKS = ("fundamental", "sentiment")


class FeatureContractMismatch(RuntimeError):
    """Raised when a model's recorded contract differs from the live one.

    This is the error the previous pipeline could not produce, and the reason
    train/serve skew went unnoticed.
    """


def block_of(feature: str) -> str | None:
    for name, feats in BLOCKS.items():
        if feature in feats:
            return name
    return None


def validate_version(model_version: int, *, model_name: str = "model") -> None:
    if model_version != FEATURE_SET_VERSION:
        raise FeatureContractMismatch(
            f"{model_name} was trained against feature set v{model_version}, "
            f"but this code defines v{FEATURE_SET_VERSION}. Retrain, or check "
            f"out the matching revision — do not predict across a mismatch."
        )


def validate_interval(model_interval: str, candle_interval: str, *,
                      model_name: str = "model") -> None:
    """The direct guard against the train/serve skew that broke the old model."""
    if (model_interval or "").upper() != (candle_interval or "").upper():
        raise FeatureContractMismatch(
            f"{model_name} was trained on {model_interval} candles but was given "
            f"{candle_interval} candles. The feature names match and the "
            f"distributions do not; refusing to predict."
        )


def validate_frame_columns(columns) -> list[str]:
    """Return contract features missing from a built frame, in contract order."""
    present = set(columns)
    return [f for f in FEATURE_NAMES if f not in present]
