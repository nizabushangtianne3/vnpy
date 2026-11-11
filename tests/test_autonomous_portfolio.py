from vnpy.autonomous.config import AllocationConfig
from vnpy.autonomous.portfolio import CapitalAllocator


def test_equal_weight_allocation() -> None:
    allocator = CapitalAllocator(AllocationConfig(method="equal"))
    result = allocator.allocate(
        total_equity=100000,
        strategies=["a", "b", "c", "d"],
    )

    # 保留 10% 现金后每策略 22500
    assert result == {"a": 22500.0, "b": 22500.0, "c": 22500.0, "d": 22500.0}


def test_equal_weight_respects_max_weight() -> None:
    allocator = CapitalAllocator(AllocationConfig(method="equal", max_weight=0.3))
    result = allocator.allocate(
        total_equity=100000,
        strategies=["a", "b"],
    )

    # 两个策略的等权本身就是 0.5，上限 0.3 无法同时满足两者，
    # 超出上限的部分保留为现金：可用 0.9 × 权重 0.6 = 0.54，
    # 保留 10% 总现金后每策略 27000
    assert result == {"a": 27000.0, "b": 27000.0}


def test_apply_bounds_clamps_extreme_weights() -> None:
    allocator = CapitalAllocator(AllocationConfig(method="equal", max_weight=0.5))
    bounded = allocator._apply_bounds({"a": 0.9, "b": 0.05})

    # 水填算法：a 被钉在上限 0.5，剩余 0.5 分给 b
    assert bounded["a"] <= 0.5
    assert bounded["b"] <= 0.5
    assert abs(sum(bounded.values()) - 1.0) < 1e-9


def test_inverse_volatility_prefers_calm_strategy() -> None:
    allocator = CapitalAllocator(
        AllocationConfig(method="inverse_volatility", cash_fraction=0, max_weight=1.0)
    )
    for value in (100, 101, 102, 103):
        allocator.record_equity("calm", value)
    for value in (100, 120, 90, 130):
        allocator.record_equity("wild", value)

    result = allocator.allocate(100000, ["calm", "wild"])

    assert result["calm"] > result["wild"]
    assert abs(sum(result.values()) - 100000) < 0.02


def test_zero_volatility_falls_back_to_equal() -> None:
    allocator = CapitalAllocator(
        AllocationConfig(method="inverse_volatility", cash_fraction=0)
    )
    for value in (100, 100, 100):
        allocator.record_equity("flat", value)

    result = allocator.allocate(100000, ["flat", "other"])
    assert result == {"flat": 50000.0, "other": 50000.0}


def test_allocate_rejects_invalid_equity() -> None:
    allocator = CapitalAllocator(AllocationConfig())
    assert allocator.allocate(0, ["a"]) == {}
    assert allocator.allocate(100000, []) == {}


def test_snapshot_reports_samples_and_volatility() -> None:
    allocator = CapitalAllocator(AllocationConfig(vol_window=5))
    allocator.register_strategy("a")
    for value in (100, 101, 102):
        allocator.record_equity("a", value)

    snapshot = allocator.snapshot()
    assert snapshot["a"]["samples"] == 3
    assert snapshot["a"]["last_equity"] == 102
    assert snapshot["a"]["volatility"] > 0
