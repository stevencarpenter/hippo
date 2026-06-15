"""Bounded, rarity-weighted node->node related edges.

Raw shared-entity co-occurrence is unusable (56M pairs live, dominated by hub
entities like 'git' that link ~42% of the KB). We exclude entities above a
degree cap and weight the rest by inverse degree so rare shared entities (a
specific file, a specific error) dominate, then keep top-K neighbours.
"""

from __future__ import annotations

import math
from collections import defaultdict


def compute_related(
    node_entities: dict[int, set],
    entity_degree: dict,
    hub_degree_cap: int,
    top_k: int,
) -> dict[int, list[int]]:
    """Return {node_id: [neighbour_node_id, ...]} bounded to top_k by rarity weight."""
    # Invert: rare (non-hub) entity -> nodes carrying it.
    entity_nodes: dict = defaultdict(list)
    for node_id, ents in node_entities.items():
        for e in ents:
            if entity_degree.get(e, 0) <= hub_degree_cap:
                entity_nodes[e].append(node_id)

    scores: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for e, nodes in entity_nodes.items():
        weight = 1.0 / math.log(1 + entity_degree[e])
        for i, a in enumerate(nodes):
            for b in nodes[i + 1 :]:
                scores[a][b] += weight
                scores[b][a] += weight

    related: dict[int, list[int]] = {}
    for node_id in node_entities:
        neighbours = scores.get(node_id, {})
        ranked = sorted(neighbours.items(), key=lambda kv: (-kv[1], kv[0]))
        related[node_id] = [nid for nid, _ in ranked[:top_k]]
    return related
