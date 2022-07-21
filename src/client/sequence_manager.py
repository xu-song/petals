from __future__ import annotations

import enum
import random
import threading
from typing import List, Optional, Sequence, Tuple, Union

from hivemind import DHT, P2P, DHTExpiration, MSGPackSerializer
from hivemind.moe.client.remote_expert_worker import RemoteExpertWorker
from hivemind.proto import runtime_pb2
from hivemind.utils.logging import get_logger, use_hivemind_log_handler

from src.data_structures import ModuleUID, RemoteModuleInfo, RemoteSpanInfo, ServerState
from src.dht_utils import get_remote_module_infos
from src.server.handler import TransformerConnectionHandler

use_hivemind_log_handler("in_root_logger")
logger = get_logger(__file__)


class RoutingStrategy(enum.Enum):
    RANDOM = enum.auto()  # choose a random compatible server at each branch and include all layers served by it
    FASTEST = enum.auto()  # [WIP] minimize the estimated time to process a given number of tokens, including latency
    LOAD_BALANCED = enum.auto()  # [WIP] use servers in proportion to their speed, on average over many sequences


class RemoteSequenceManager(threading.Thread):
    """
    Sequence manager is a thread that keeps track of information on remote servers that constitute a RemoteSequential.
    TL;DR it tells you, which peers you should ask to get a specific layer. It is used in RemoteSequential.

    When created, RemoteSequenceManager looks up which servers serve necessary layers by reading from DHT.
    Using this information, sequence manager can form sequences of servers that collectively have the full sequence.

    To form such a sequence, call .make_sequence with the appropriate optimization policy (see make_sequence docstr).

    :note: RemoteSequenceManager takes up some CPU and network I/O to operate in background. It is recommended to avoid
      running redundant sequence managers for the same set of layers.

    Example
    =======
    >>> sequence_manager = RemoteSequenceManager(dht=..., block_uids=('me/my-model.0', 'me/my-model.1', 'me/my-model.2')
    >>> seq1_full_model = sequence_manager.make_sequence()
    >>> seq2_partial = sequence_manager.make_sequence(start_index=0, end_index=2)  # the end index is exclusive
    >>> seq1_fastest = sequence_manager.make_sequence()

    """

    def __init__(
        self,
        dht: DHT,
        block_uids: Sequence[ModuleUID],
        *,
        p2p: Optional[P2P] = None,
        start: bool,
        max_retries: int = 3,
        update_period: float = 30,
    ):  # NB: if you add any more parameters, please make sure you pass them to sub-sequences in .__getitem__ below!
        super().__init__(daemon=True)
        self.dht, self.p2p = dht, (p2p if p2p is not None else dht.replicate_p2p())
        self.block_uids: List[ModuleUID] = list(block_uids)
        self.block_infos: List[Optional[RemoteModuleInfo]] = [None] * len(self.block_uids)
        self.spans_by_priority: List[RemoteSpanInfo] = []  # sorted from best to worst
        self.spans_containing_block: Tuple[List[RemoteSpanInfo], ...] = tuple([] for _ in range(len(self.block_uids)))

        self.update_period, self.max_retries = update_period, max_retries
        self.last_update_time: DHTExpiration = -float("inf")

        self._rpc_info = None
        self._lock_changes = threading.Lock()
        self.ready = threading.Event()  # whether or not this thread is ready to make_sequence

        if start:
            self.run_in_background()

        for uid, info in zip(self.block_uids, self.block_infos):
            assert info is not None, f"Found no remote peers for block {uid}"
        assert self.spans_by_priority and self.spans_containing_block

    def make_sequence(
        self,
        start_index: int = 0,
        end_index: Optional[int] = None,
        strategy: RoutingStrategy = RoutingStrategy.RANDOM,
        num_tokens: Optional[int] = None,
    ) -> Sequence[RemoteSpanInfo]:
        """
        Form a sequence of remote servers that collectively serve all consecutive layers

        :param start_index: optional index of the first module in a sequence, default = the first of block_uids
        :param end_index: optional index of the last module (non-inclusive), default = after last of block_uids
        :param strategy: the routing algorithm to use (e.g. random, fastest, balanced), see RoutingStrategy for details
        :param num_tokens: the number of tokens sent through this sequence at a time, used by RoutingStrategy.FASTEST
        """
        assert self.is_alive()
        if not self.ready.is_set():
            logger.warning(f"{self.__class__.__name__} is still initializing, waiting until it's ready...")
            self.ready.wait()
            logger.warning(f"Finished waiting for {self.__class__.__name__} to initialize")
        if (strategy is RoutingStrategy.FASTEST) != (num_tokens is not None):
            logger.warning("please specify num_tokens with FASTEST strategy (and only with FASTEST strategy)")
        end_index = end_index if end_index is not None else len(self.block_uids)

        if strategy == RoutingStrategy.RANDOM:
            span_sequence = []
            current_index = start_index
            while current_index < end_index:
                candidate_spans = self.spans_containing_block[current_index]
                chosen_span = random.choice(candidate_spans)
                assert chosen_span.start <= current_index < chosen_span.end
                span_sequence.append(chosen_span)
                current_index = chosen_span.end
            return span_sequence
        elif strategy == RoutingStrategy.FASTEST:
            raise NotImplementedError("Fastest routing strategy is not implemented (yet)")
        elif strategy == RoutingStrategy.LOAD_BALANCED:
            raise NotImplementedError("Load-balanced routing strategy is not implemented (yet)")




    def __getitem__(self, ix: Union[int, slice]) -> RemoteSequenceManager:
        """Get a RemoteSequenceManager for a sub-sequence of blocks"""
        assert isinstance(ix, (int, slice))
        if not isinstance(ix, slice):
            ix = slice(int(ix), int(ix) + 1, 1)

        self.ready.wait()
        with self._lock_changes:
            subseq = RemoteSequenceManager(
                self.dht,
                self.block_uids[ix],
                p2p=self.p2p,
                max_retries=self.max_retries,
                update_period=self.update_period,
                start=False,
            )  # NB: if you've added more parameters to __init__, please forward them in the instantiation above
            subseq.block_infos = self.block_infos[ix]
            subseq.spans_by_priority, subseq.spans_containing_block = subseq.compute_spans(subseq.block_infos)
            subseq._rpc_info = self._rpc_info
            subseq.last_update_time = self.last_update_time
            if self.is_alive():
                subseq.run_in_background()
        return subseq

    def update_(self):
        with self._lock_changes:
            self.update_block_infos_()
            self.spans_by_priority, self.spans_containing_block = self.compute_spans(self.block_infos)

    def run_in_background(self, await_ready: bool = True, timeout: Optional[float] = None) -> None:
        """
        Starts averager in a background process. if await_ready, this method will wait until background dht
        is ready to process incoming requests or for :timeout: seconds max.
        """
        self.start()
        if await_ready:
            self.ready.wait(timeout)

    def update_block_infos_(self):
        new_block_infos = get_remote_module_infos(self.dht, self.block_uids, expiration_time=float("inf"))
        assert len(new_block_infos) == len(self.block_uids)
        for block_index, (uid, info) in enumerate(zip(self.block_uids, new_block_infos)):
            if info is None:
                logger.warning(f"Found no block info for block {uid}")
            if not isinstance(info, RemoteModuleInfo):
                logger.warning(f"Unexpected dht entry type for {uid}: {info}")
            if not info.servers:
                logger.warning(f"Found no active peers for block {uid}")
            if info.uid != uid:
                logger.warning(f"The DHT entry for {uid} actually points to {info.uid}")
            self.block_infos[block_index] = info

    @staticmethod
    def compute_spans(block_infos: Sequence[RemoteModuleInfo]):
        closed_spans = []
        active_spans = {}
        for block_index, info in enumerate(block_infos):
            for peer_id, server in info.servers.items():
                if server.state != ServerState.ONLINE:
                    continue
                if peer_id not in active_spans:
                    active_spans[peer_id] = RemoteSpanInfo(start=block_index, end=block_index + 1, peer_id=peer_id)
                else:  # peer_id in active_spans
                    active_spans[peer_id].end = block_index + 1

            for peer_id in list(active_spans.keys()):
                if (
                    peer_id not in info.servers
                    or info.servers[peer_id].state != ServerState.ONLINE
                    or block_index == len(block_infos) - 1
                ):
                    closed_spans.append(active_spans.pop(peer_id))
        assert not active_spans

        closed_spans.sort(key=lambda span: span.end - span.start, reverse=True)

        spans_containing_block = tuple(list() for _ in range(len(block_infos)))
        for span in closed_spans:
            for block_index in range(span.start, span.end):
                spans_containing_block[block_index].append(span)

        return closed_spans, spans_containing_block

    def __len__(self):
        return len(self.block_uids)

    @property
    def rpc_info(self):
        """Return the rpc_info queried from one of the servers that hold the first block"""
        if self._rpc_info is None:
            retries = 0
            for i in range(self.max_retries):
                try:
                    self.update_()
                    peer_id = random.choice(list(self.block_infos[0].servers.keys()))
                    stub = TransformerConnectionHandler.get_stub(self.p2p, peer_id)
                    outputs = RemoteExpertWorker.run_coroutine(
                        stub.rpc_info(runtime_pb2.ExpertUID(uid=self.block_uids[0]))
                    )
                    self._rpc_info = MSGPackSerializer.loads(outputs.serialized_info)
                except Exception as e:
                    retries += 1
                    if retries >= self.max_retries:
                        raise e
                    else:
                        logger.warning(f"Tried to call rpc_info, but caught {repr(e)}", exc_info=True)
        return self._rpc_info
