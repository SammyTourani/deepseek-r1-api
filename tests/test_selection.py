"""Unit tests for the pure node-pool selection / scaling logic.

These exercise the routing and scaling *decisions* the proxy and scaler make,
with no AWS, network, or environment dependencies.
"""
import pytest

from selection import (
    can_scale_down,
    can_scale_up,
    eject_node,
    pick_node_to_remove,
    select_round_robin,
)


def _node(node_id, **extra):
    return {"node_id": node_id, "ip": f"10.0.0.{node_id[-1]}", **extra}


# ─── select_round_robin ─────────────────────────────────────────────────────


def test_round_robin_cycles_through_pool_in_order():
    nodes = [_node("a"), _node("b"), _node("c")]
    picked = [select_round_robin(nodes, c)["node_id"] for c in range(3)]
    assert picked == ["a", "b", "c"]


def test_round_robin_wraps_around_when_counter_exceeds_pool_size():
    nodes = [_node("a"), _node("b")]
    # counters 0..5 -> a, b, a, b, a, b
    picked = [select_round_robin(nodes, c)["node_id"] for c in range(6)]
    assert picked == ["a", "b", "a", "b", "a", "b"]


def test_round_robin_distributes_evenly_over_full_cycles():
    nodes = [_node(f"n{i}") for i in range(4)]
    counts = {n["node_id"]: 0 for n in nodes}
    for c in range(40):  # 10 full cycles
        counts[select_round_robin(nodes, c)["node_id"]] += 1
    assert set(counts.values()) == {10}


def test_round_robin_single_node_always_selected():
    nodes = [_node("solo")]
    assert all(select_round_robin(nodes, c)["node_id"] == "solo" for c in range(5))


def test_round_robin_handles_large_warm_counter_without_overflow():
    # Simulates a long-lived warm Lambda whose global counter never resets.
    nodes = [_node("a"), _node("b"), _node("c")]
    assert select_round_robin(nodes, 1_000_000)["node_id"] == "b"  # 1_000_000 % 3 == 1


def test_round_robin_negative_counter_maps_into_range():
    nodes = [_node("a"), _node("b"), _node("c")]
    # Python modulo keeps the index non-negative.
    assert select_round_robin(nodes, -1)["node_id"] == "c"


def test_round_robin_empty_pool_raises():
    with pytest.raises(ValueError):
        select_round_robin([], 0)


# ─── eject_node ──────────────────────────────────────────────────────────────


def test_eject_removes_only_the_named_node():
    nodes = [_node("a"), _node("b"), _node("c")]
    remaining = eject_node(nodes, "b")
    assert [n["node_id"] for n in remaining] == ["a", "c"]


def test_eject_does_not_mutate_the_input_list():
    nodes = [_node("a"), _node("b")]
    eject_node(nodes, "a")
    assert [n["node_id"] for n in nodes] == ["a", "b"]


def test_eject_unknown_node_is_a_noop():
    nodes = [_node("a"), _node("b")]
    assert eject_node(nodes, "does-not-exist") == nodes


def test_eject_last_node_yields_empty_list():
    assert eject_node([_node("only")], "only") == []


def test_eject_removes_all_matching_ids():
    # Defensive: duplicate ids should all be dropped.
    nodes = [_node("a"), _node("a"), _node("b")]
    assert [n["node_id"] for n in eject_node(nodes, "a")] == ["b"]


def test_eject_tolerates_node_without_id_field():
    nodes = [{"ip": "10.0.0.9"}, _node("a")]
    # The id-less node is not the target, so it survives.
    assert eject_node(nodes, "a") == [{"ip": "10.0.0.9"}]


# ─── can_scale_up / can_scale_down ───────────────────────────────────────────


@pytest.mark.parametrize(
    "current, max_nodes, expected",
    [
        (0, 4, True),
        (3, 4, True),
        (4, 4, False),  # at cap
        (5, 4, False),  # over cap (defensive)
    ],
)
def test_can_scale_up_respects_max(current, max_nodes, expected):
    assert can_scale_up(current, max_nodes) is expected


@pytest.mark.parametrize(
    "healthy, min_nodes, expected",
    [
        (4, 1, True),
        (2, 1, True),
        (1, 1, False),  # at floor
        (0, 1, False),  # below floor (defensive)
    ],
)
def test_can_scale_down_respects_min(healthy, min_nodes, expected):
    assert can_scale_down(healthy, min_nodes) is expected


def test_scale_bounds_are_complementary_in_steady_state():
    # With min < count < max, the pool can move in either direction.
    assert can_scale_up(2, 4) and can_scale_down(2, 1)


# ─── pick_node_to_remove ─────────────────────────────────────────────────────


def test_pick_node_to_remove_chooses_least_recently_checked():
    nodes = [
        _node("new", last_checked="2024-03-01T12:00:00Z"),
        _node("old", last_checked="2024-01-01T00:00:00Z"),
        _node("mid", last_checked="2024-02-15T06:30:00Z"),
    ]
    assert pick_node_to_remove(nodes)["node_id"] == "old"


def test_pick_node_to_remove_prefers_never_checked_node():
    # A node missing last_checked sorts first (empty string) and is removed.
    nodes = [
        _node("checked", last_checked="2024-01-01T00:00:00Z"),
        _node("never"),  # no last_checked
    ]
    assert pick_node_to_remove(nodes)["node_id"] == "never"


def test_pick_node_to_remove_single_node():
    assert pick_node_to_remove([_node("solo", last_checked="2024-01-01T00:00:00Z")])[
        "node_id"
    ] == "solo"


def test_pick_node_to_remove_empty_pool_raises():
    with pytest.raises(ValueError):
        pick_node_to_remove([])
