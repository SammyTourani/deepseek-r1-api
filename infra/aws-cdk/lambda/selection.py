"""
selection.py — Pure node-pool selection / scaling helpers
=========================================================
Side-effect-free logic shared by the proxy (round-robin load balancing,
unhealthy-node ejection) and the scaler (scale bounds, scale-down target
selection).

This module deliberately has **no** boto3, network, or environment
dependencies so the routing and scaling decisions can be reasoned about and
unit-tested in isolation from AWS.

A "node" is a plain ``dict`` as stored in DynamoDB, e.g.::

    {"node_id": "i-123", "ip": "10.0.0.1", "port": 8000,
     "status": "healthy", "last_checked": "2024-01-01T00:00:00Z"}
"""
from __future__ import annotations

from typing import Sequence


def select_round_robin(nodes: Sequence[dict], counter: int) -> dict:
    """Pick a node by round-robin using a monotonically increasing ``counter``.

    The caller owns the counter (a warm-Lambda global) and increments it once
    per selection. Indexing is modulo the pool size so the counter may grow
    without bound and negative counters still map into range.

    Raises ``ValueError`` if the pool is empty, matching the proxy contract that
    routing is only attempted when at least one healthy node exists.
    """
    if not nodes:
        raise ValueError("cannot select from an empty node pool")
    return nodes[counter % len(nodes)]


def eject_node(nodes: Sequence[dict], node_id: str) -> list[dict]:
    """Return a new list with every node matching ``node_id`` removed.

    Used by the proxy to drop a node that failed mid-request from the local
    candidate list for the remainder of the invocation, without mutating the
    caller's list.
    """
    return [n for n in nodes if n.get("node_id") != node_id]


def can_scale_up(current_count: int, max_nodes: int) -> bool:
    """True when another node may be added without exceeding ``max_nodes``."""
    return current_count < max_nodes


def can_scale_down(healthy_count: int, min_nodes: int) -> bool:
    """True when a node may be removed while staying at or above ``min_nodes``."""
    return healthy_count > min_nodes


def pick_node_to_remove(nodes: Sequence[dict]) -> dict:
    """Choose the scale-down victim: the least-recently-checked healthy node.

    Nodes are ordered by their ``last_checked`` ISO-8601 timestamp (lexical sort
    is chronological for that format); a missing timestamp sorts first so a node
    that was never health-checked is preferred for removal.

    Raises ``ValueError`` on an empty pool.
    """
    if not nodes:
        raise ValueError("cannot pick a node to remove from an empty pool")
    return sorted(nodes, key=lambda n: n.get("last_checked", ""))[0]
