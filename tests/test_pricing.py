"""Pricing/tiering tests. No API calls -- pure config.estimate_request_cost /
config.tier_label logic against config/pricing.json.

claude-haiku-5-5 is priced per request by that request's own prompt size:
<=100,000 input tokens -> $0.10 in / $0.50 out per MTok; >100,000 -> $0.50 in /
$2.50 out per MTok. claude-haiku-4-5 stays flat at $1 / $5 regardless of size.
"""

import pytest

from agent import config


def test_haiku_5_5_short_prompt_uses_cheap_tier():
    assert config.tier_label("claude-haiku-5-5", 1000) == "<=100k"
    cost = config.estimate_request_cost("claude-haiku-5-5", 50_000, 2_000)
    assert cost == pytest.approx(0.006)  # 50_000/1e6*0.10 + 2_000/1e6*0.50


def test_haiku_5_5_long_prompt_uses_expensive_tier():
    assert config.tier_label("claude-haiku-5-5", 500_000) == ">100k"
    cost = config.estimate_request_cost("claude-haiku-5-5", 200_000, 10_000)
    assert cost == pytest.approx(0.125)  # 200_000/1e6*0.50 + 10_000/1e6*2.50


def test_haiku_5_5_boundary_at_exactly_100k_is_cheap_tier():
    # "<=100,000 input tokens" -- the boundary value itself is in the cheap tier.
    assert config.tier_label("claude-haiku-5-5", 100_000) == "<=100k"
    cost = config.estimate_request_cost("claude-haiku-5-5", 100_000, 0)
    assert cost == pytest.approx(0.01)  # 100_000/1e6*0.10


def test_haiku_5_5_boundary_at_100k_plus_one_is_expensive_tier():
    assert config.tier_label("claude-haiku-5-5", 100_001) == ">100k"
    cost = config.estimate_request_cost("claude-haiku-5-5", 100_001, 0)
    # 100_001/1e6*0.50 == 0.0500005, which sits exactly on round()'s 6-decimal
    # halfway point -- allow the +-5e-7 that rounding itself can introduce.
    assert cost == pytest.approx(100_001 / 1_000_000 * 0.50, abs=1e-6)
    # sanity: must be priced at the expensive tier's rate, not the cheap one
    assert cost > config.estimate_request_cost("claude-haiku-5-5", 100_000, 0)


def test_haiku_5_5_tier_is_per_request_not_cumulative():
    # Two small per-request calls, each well under the 100k threshold, must
    # each be billed at the cheap tier -- summing them must NOT equal what a
    # single combined >100k request would cost.
    per_call = config.estimate_request_cost("claude-haiku-5-5", 60_000, 0)
    two_calls_total = per_call * 2
    one_big_call = config.estimate_request_cost("claude-haiku-5-5", 120_000, 0)
    assert two_calls_total < one_big_call


def test_haiku_4_5_is_flat_regardless_of_prompt_size():
    assert config.tier_label("claude-haiku-4-5", 1_000) is None
    assert config.tier_label("claude-haiku-4-5", 500_000) is None

    small = config.estimate_request_cost("claude-haiku-4-5", 1_000, 1_000)
    large = config.estimate_request_cost("claude-haiku-4-5", 500_000, 1_000)
    # same per-MTok rate applies at any size: $1 in / $5 out
    assert small == pytest.approx(1_000 / 1_000_000 * 1.00 + 1_000 / 1_000_000 * 5.00)
    assert large == pytest.approx(500_000 / 1_000_000 * 1.00 + 1_000 / 1_000_000 * 5.00)


def test_unknown_model_raises():
    with pytest.raises(KeyError):
        config.estimate_request_cost("claude-made-up-model", 100, 100)
