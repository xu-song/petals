from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
from hivemind import PeerID, get_logger

from src.data_structures import RemoteModuleInfo, ServerState

__all__ = ["choose_best_blocks", "should_choose_other_blocks"]

logger = get_logger(__file__)


@dataclass
class Span:
    start: int
    end: int
    throughput: float

    @property
    def length(self):
        return self.end - self.start

    def move_to(self, new_start: int) -> None:
        self.start, self.end = new_start, new_start + self.length


def _compute_spans(module_infos: List[Optional[RemoteModuleInfo]]) -> Tuple[Dict[PeerID, Span], np.ndarray]:
    spans = {}
    throughputs = np.zeros(len(module_infos))
    for block, module in enumerate(module_infos):
        if module is None:
            continue

        for peer_id, server in module.servers.items():
            if server.state == ServerState.OFFLINE:
                continue

            if peer_id in spans:
                spans[peer_id].start = min(spans[peer_id].start, block)
                spans[peer_id].end = max(spans[peer_id].start, block + 1)
            else:
                spans[peer_id] = Span(start=block, end=block + 1, throughput=server.throughput)

            throughputs[block] += server.throughput

    return spans, throughputs


def _choose_best_start(throughputs: np.ndarray, num_blocks: int, cur_start: Optional[int]) -> int:
    options = (
        (sorted(throughputs[i : i + num_blocks]), i != cur_start, i)
        for i in range(0, len(throughputs) - num_blocks + 1)
    )
    return min(options)[-1]


def choose_best_blocks(num_blocks: int, module_infos: List[Optional[RemoteModuleInfo]]) -> List[int]:
    _, throughputs = _compute_spans(module_infos)
    start = _choose_best_start(throughputs, num_blocks, None)
    return list(range(start, start + num_blocks))


def should_choose_other_blocks(
    local_peer_id: PeerID, module_infos: List[Optional[RemoteModuleInfo]], min_balance_quality: float
) -> bool:
    if min_balance_quality > 1.0:
        return True  # Forces rebalancing on each check (may be used for debugging purposes)

    spans, throughputs = _compute_spans(module_infos)
    initial_throughput = throughputs.min()

    assert local_peer_id in spans, "Span served by this server is not present in the DHT"
    local_span = spans[local_peer_id]
    throughputs[local_span.start : local_span.end] -= local_span.throughput

    new_start = _choose_best_start(throughputs, local_span.length, local_span.start)
    if local_span.start == new_start:
        return False  # This server is on its best place already
    local_span.move_to(new_start)

    throughputs[local_span.start : local_span.end] += local_span.throughput

    moved = True
    while moved:
        servers = list(spans.keys())
        np.random.shuffle(servers)

        moved = False
        for peer_id in servers:
            span = spans[peer_id]
            throughputs[span.start : span.end] -= span.throughput

            new_start = _choose_best_start(throughputs, span.length, span.start)
            if span.start != new_start:
                span.move_to(new_start)
                moved = True

            throughputs[span.start : span.end] += span.throughput

    new_throughput = throughputs.min()
    balance_quality = initial_throughput / new_throughput
    logger.info(f"Swarm balance quality: {balance_quality * 100:.1f}%")

    eps = 1e-6
    return balance_quality < min_balance_quality - eps
