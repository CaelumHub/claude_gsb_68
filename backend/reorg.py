"""Reorg history: a persistent log of chain reorganisations.

Whenever the heaviest-chain rule makes the node abandon a segment of its main
chain and switch onto a competing branch, an event is appended to
``reorgs.json`` in the node's data directory.  Each event captures everything
the reorg-history view needs to reconstruct the moment of the switch:

* the fork point (common ancestor height/hash),
* the chain tip before and after the switch,
* every abandoned block (with its original height) and every applied block.

Combined with the live fork store (rival branches that have not won — yet),
this lets the UI draw where chains diverged, how far a rival chain grew, and
which reorg orphaned any given block.
"""

import time

from .storage import atomic_write_json, read_json

MAX_REORG_EVENTS = 100


def summarize_block(block):
    """Compact, JSON-safe description of a block for the reorg views."""
    miner = None
    if block.transactions and block.transactions[0].is_coinbase():
        miner = block.transactions[0].to
    return {
        "index": block.index,
        "hash": block.hash,
        "prev_hash": block.prev_hash,
        "timestamp": block.timestamp,
        "difficulty": block.difficulty,
        "nonce": block.nonce,
        "tx_count": block.display_tx_count(),
        "miner": miner,
    }


class ReorgLog:
    """Append-only, size-capped ledger of reorg events, persisted as JSON."""

    def __init__(self, path, max_events=MAX_REORG_EVENTS):
        self.path = path
        self._max_events = max_events
        self.events = read_json(path, [])

    def record(self, fork_height, fork_hash, old_head, new_head,
               abandoned, applied):
        """Record one reorganisation.

        ``old_head`` / ``new_head`` are :class:`~block.Block` objects (the tip
        before and after the switch); ``abandoned`` and ``applied`` are lists
        of blocks.  Returns the stored event.
        """
        last_id = self.events[-1].get("id", 0) if self.events else 0
        event = {
            "id": last_id + 1,
            "time": time.time(),
            "fork_height": fork_height,
            "fork_hash": fork_hash,
            "old_head": {"index": old_head.index, "hash": old_head.hash},
            "new_head": {"index": new_head.index, "hash": new_head.hash},
            "depth": len(abandoned),
            "abandoned": [summarize_block(b) for b in abandoned],
            "applied": [summarize_block(b) for b in applied],
        }
        self.events.append(event)
        if len(self.events) > self._max_events:
            self.events = self.events[-self._max_events:]
        atomic_write_json(self.path, self.events)
        return event

    def all(self):
        return list(self.events)

    def orphaned_blocks(self):
        """Flat list of ``(event, block_summary)`` for every orphaned block."""
        out = []
        for ev in self.events:
            for b in ev.get("abandoned", []):
                out.append((ev, b))
        return out
