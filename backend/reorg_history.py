"""Persistent history of chain reorganisations and the blocks they discard.

The consensus core (:mod:`backend.blockchain`) only keeps blocks reachable
from the current chain head plus a small in-memory ``fork_store``; once a
branch loses a fork race its blocks disappear from every view.  This module is
the *audit trail* that keeps them visible:

* ``events``  — one ordered entry per reorg (fork point, old/new heads, which
  blocks were rolled back and which were adopted);
* ``orphans`` — every block that ever belonged to a main chain and was later
  abandoned by a reorg, keyed by hash, tagged with the reorg id that dropped
  it (and the reorg id that later brought it back, if that happened).

Both lists are stored as JSON (``reorgs.json``) and rewritten atomically, so
the history survives node restarts and can back a UI without the consensus
core having to retain dead blocks.
"""

import time

from .storage import atomic_write_json, read_json


class ReorgHistory:
    def __init__(self, path, max_events=200):
        self.path = path
        self.max_events = max_events
        data = read_json(path, {"events": [], "orphans": {}})
        if not isinstance(data, dict):
            data = {"events": [], "orphans": {}}
        self.events = data.get("events", [])
        # Keyed by block hash -> orphan record (plain dicts).
        self.orphans = data.get("orphans", {})
        self._next_id = 1 + max((e.get("id", 0) for e in self.events),
                                default=0)

    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #
    def record(self, fork_height, abandoned_blocks, adopted_blocks,
               old_head=None, new_head=None, reason="heavier fork",
               chainwork_before=0, chainwork_after=0):
        """Append a reorg event and update the abandoned-block registry.

        ``abandoned_blocks`` / ``adopted_blocks`` are lists of compact block
        dicts (see :meth:`Blockchain.compact_block`).  Blocks are first marked
        readopted (in case they are returning to the main chain) and then the
        ones this reorg drops are registered as orphans.
        """
        event_id = self._next_id
        self._next_id += 1
        now = time.time()

        # A block adopted by this reorg may have been orphaned by an earlier
        # one: mark it as back on the main chain instead of leaving a stale
        # "discarded" label on it.
        for b in adopted_blocks:
            rec = self.orphans.get(b["hash"])
            if rec is not None:
                rec["status"] = "readopted"
                rec["readopted_by"] = event_id

        for b in abandoned_blocks:
            self.orphans[b["hash"]] = {
                "height": b["index"],
                "hash": b["hash"],
                "prev_hash": b["prev_hash"],
                "timestamp": b.get("timestamp"),
                "difficulty": b.get("difficulty"),
                "tx_count": b.get("tx_count", 0),
                "nonce": b.get("nonce", 0),
                "merkle_root": b.get("merkle_root"),
                "state_root": b.get("state_root"),
                "status": "abandoned",
                "abandoned_by": event_id,
                "readopted_by": None,
                "abandoned_at": now,
            }

        event = {
            "id": event_id,
            "time": now,
            "fork_height": fork_height,
            "reason": reason,
            "old_head": _head_info(old_head),
            "new_head": _head_info(new_head),
            "abandoned": abandoned_blocks,
            "adopted": adopted_blocks,
            "rollback_count": len(abandoned_blocks),
            "applied_count": len(adopted_blocks),
            "chainwork_before": chainwork_before,
            "chainwork_after": chainwork_after,
        }
        self.events.append(event)
        if len(self.events) > self.max_events:
            self.events = self.events[-self.max_events:]
        self.save()
        return event

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def all_events(self):
        """Reorg events, newest first."""
        return list(reversed(self.events))

    def orphan(self, hash_hex):
        return self.orphans.get(hash_hex)

    def orphan_summary(self):
        """Flat list of orphan records, newest abandonment first."""
        recs = list(self.orphans.values())
        recs.sort(key=lambda r: (r.get("abandoned_at") or 0,
                                 r.get("height") or 0), reverse=True)
        return recs

    def is_readopted(self, hash_hex):
        rec = self.orphans.get(hash_hex)
        return bool(rec and rec.get("status") == "readopted")

    def readopted_by(self, hash_hex):
        rec = self.orphans.get(hash_hex)
        return rec.get("readopted_by") if rec else None

    def abandoned_by(self, hash_hex):
        rec = self.orphans.get(hash_hex)
        return rec.get("abandoned_by") if rec else None

    def clear(self):
        self.events = []
        self.orphans = {}
        self._next_id = 1
        self.save()

    def save(self):
        atomic_write_json(self.path, {
            "events": self.events,
            "orphans": self.orphans,
            "updated_at": time.time(),
        })


def _head_info(block):
    if block is None:
        return None
    return {"height": block.get("index") if isinstance(block, dict)
            else getattr(block, "index", None),
            "hash": block.get("hash") if isinstance(block, dict)
            else getattr(block, "hash", None)}
