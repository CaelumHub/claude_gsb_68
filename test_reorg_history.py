"""Exercise the reorg-history feature end to end.

Scenario (constant low difficulty, coinbase-only blocks):

    G ─ A1 ─ A2 ─ A3 ─ A4                 (main chain, height 4)
          └─ B2 ─ B3 ─ B4 ─ B5            (stored competing branch until B5
                                           makes it heavier -> reorg R#1,
                                           A2/A3/A4 are discarded)

    after R#1, A2 re-arrives (as if gossiped by a peer still holding it) and
    a heavier branch descends from it:

    G ─ A1 ─ B2 ─ B3 ─ B4 ─ B5            (main after R#1)
          └─ A2 ─ C3 ─ C4 ─ C5 ─ C6       (heavier -> reorg R#2:
                                           B2..B5 discarded, A2 readopted)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import pow as pow_mod
from backend.block import Block
from backend.blockchain import Blockchain
from backend.server import create_app
from backend.state import WorldState, ZERO_ADDRESS
from backend.storage import DataPaths, read_json
from backend.transaction import create_coinbase
from backend.config import COINBASE_REWARD

DIFF = 4.0


def craft(bc, index, parent_hash, parent_state):
    coinbase = create_coinbase(ZERO_ADDRESS, COINBASE_REWARD, index)
    block = Block(index, parent_hash, [coinbase])
    block.difficulty = DIFF
    block.timestamp = 1_000_000 + index * 10  # deterministic, increasing
    block._recompute_header()
    new_state, _ = bc.apply_block(block, parent_state.copy())
    block.set_state_root(new_state.root())
    pow_mod.mine(block, DIFF)
    return block, new_state


def main():
    tmp = tempfile.mkdtemp(prefix="lc-reorg-test-")
    cfg = {"node_id": "tester", "INITIAL_DIFFICULTY_BITS": DIFF,
           "data_dir": tmp}
    paths = DataPaths(tmp, cfg)
    bc = Blockchain(cfg, paths)
    bc.genesis_difficulty = DIFF
    bc.create_genesis()

    G = bc.get_block(0)
    sG = bc.state.copy()

    # --- main chain A1..A4 ---
    states = {G.hash: sG}
    prev_h, prev_s = G.hash, sG
    A = {}
    for i in range(1, 5):
        b, s = craft(bc, i, prev_h, prev_s)
        status, msg = bc.add_block(b)
        assert status == "extended", (i, status, msg)
        A[i] = b
        states[b.hash] = s
        prev_h, prev_s = b.hash, s

    # --- competing branch B2..B4 stays a stored (visible) fork ---
    B = {}
    bh, bs = A[1].hash, states[A[1].hash]
    for i in range(2, 5):
        b, s = craft(bc, i, bh, bs)
        B[i] = b
        status, msg = bc.add_block(b)
        assert status == "stored_fork", (i, status, msg)
        bh, bs = b.hash, s

    branches = bc.active_fork_branches()
    assert len(branches) == 1, branches
    br = branches[0]
    assert br["status"] == "complete" and br["length"] == 3, br
    assert br["fork_point"] == {"height": 1, "hash": A[1].hash}, br
    print("OK active competing branch: fork at #1, length 3 (B2..B4)")

    # --- B5 makes the branch heavier -> reorg R#1 ---
    b5, _ = craft(bc, 5, bh, bs)
    B[5] = b5
    status, msg = bc.add_block(b5)
    assert status == "reorg", (status, msg)
    assert "reorg #1" in msg, msg
    assert [b.index for b in bc.chain] == [0, 1, 2, 3, 4, 5]
    assert bc.get_block(2).hash == B[2].hash
    assert not bc.has_block(A[2].hash)  # A2 dropped from chain + fork_store
    print("OK", msg)

    # --- A2 re-arrives from a peer and a heavier C-branch is built on it ---
    bc.fork_store.setdefault(2, []).append(A[2])  # re-gossiped old block
    ch, cs = A[2].hash, states[A[2].hash]
    C = {}
    for i in range(3, 7):
        b, s = craft(bc, i, ch, cs)
        C[i] = b
        ch, cs = b.hash, s
        status, msg = bc.add_block(b)
        # C3..C5 extend the stored branch; C6 triggers the reorg
        assert status in ("stored_fork", "reorg"), (i, status, msg)
        if status == "reorg":
            assert i == 6 and "reorg #2" in msg, (i, msg)
            reorg_msg = msg
    print("OK", reorg_msg)
    assert bc.get_block(2).hash == A[2].hash          # A2 back on main
    assert bc.get_block(3).hash == C[3].hash
    assert bc.height == 6

    # --- history / registry checks ---
    hist = bc.reorg_history
    assert len(hist.events) == 2
    e1, e2 = hist.events
    assert e1["fork_height"] == 1
    assert [b["index"] for b in e1["abandoned"]] == [2, 3, 4]
    assert [b["hash"] for b in e1["abandoned"]] == [A[2].hash, A[3].hash, A[4].hash]
    assert e1["old_head"]["height"] == 4 and e1["new_head"]["height"] == 5
    assert [b["index"] for b in e2["abandoned"]] == [2, 3, 4, 5]
    assert hist.orphan(A[2].hash)["status"] == "readopted"
    assert hist.orphan(A[2].hash)["abandoned_by"] == 1
    assert hist.orphan(A[2].hash)["readopted_by"] == 2
    assert hist.orphan(B[2].hash)["abandoned_by"] == 2
    assert hist.orphan(A[3].hash)["abandoned_by"] == 1
    print("OK orphan registry: A2 dropped in R#1 / back in R#2; "
          "B2..B5 dropped in R#2")

    # --- persistence on disk ---
    on_disk = read_json(paths.reorgs_path)
    assert len(on_disk["events"]) == 2 and len(on_disk["orphans"]) == 7
    print("OK history persisted to", paths.reorgs_path)

    # --- HTTP API ---
    app = create_app(_NodeStub(bc))
    client = app.test_client()
    r = client.get("/api/chain/reorgs")
    assert r.status_code == 200
    data = r.get_json()
    assert data["height"] == 6
    assert [e["id"] for e in data["events"]] == [2, 1]  # newest first
    assert len(data["main_chain"]) == 7
    a2_main = next(b for b in data["main_chain"] if b["hash"] == A[2].hash)
    assert a2_main["readopted_by"] == 2
    adopted_r2 = data["events"][0]["adopted"]
    assert {b["hash"] for b in adopted_r2} == {A[2].hash} | {
        C[i].hash for i in range(3, 7)}
    # B-chain now shows as an orphan chain (not active fork)
    assert data["fork_branches"] == []
    print("OK GET /api/chain/reorgs payload")

    r = client.get("/api/block/" + A[3].hash)
    assert r.status_code == 200 and r.get_json()["orphan"] is True
    assert r.get_json()["block"]["abandoned_by"] == 1
    r = client.get("/api/block/" + A[3].hash[:12])  # short hash -> 404
    assert r.status_code == 404
    print("OK discarded block served from history with original height")

    # page is served
    assert client.get("/reorgs.html").status_code == 200
    print("OK /reorgs.html served")
    print("\nALL CHECKS PASSED")


class _NodeStub:
    """Minimal stand-in: create_app only touches node.blockchain for these
    read-only endpoints."""
    def __init__(self, bc):
        self.blockchain = bc


if __name__ == "__main__":
    main()
