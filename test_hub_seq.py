"""Unit tests for bridge/hub.py's socket registry and announcement ids, and for
tasks.py's finished-record pruning. No network, no aiohttp server, no real
WebSocket - everything is stubbed. Run: python -m pytest test_hub_seq.py

Both hub tests cover bugs that were invisible in normal use:
  - send() called _sockets.discard() on a dict, so the FAILURE path raised
    AttributeError out through broadcast() and killed the server push loop.
  - the announcement counter restarted at 1 each process, so replay to a
    reconnecting phone silently delivered nothing after a bridge restart.
"""
import asyncio
import itertools
import json
import unittest
from unittest.mock import patch

from bridge import hub, util
import tasks


SNAP = {"type": "state", "ts": 1000.0,
        "cortana": {"state": "idle", "thoughts": []},
        "board": {"tasks": []},
        "devices": [{"id": "a", "name": "Pixel", "last_seen": 500.0, "online": True}]}


def snap(**over):
    s = json.loads(json.dumps(SNAP))     # deep copy
    s.update(over)
    return s


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class GoodSocket:
    def __init__(self):
        self.sent = []

    async def send_str(self, msg):
        self.sent.append(msg)


class DeadSocket:
    """A phone that dropped between two heartbeats - the normal failure."""
    async def send_str(self, msg):
        raise ConnectionResetError("peer went away")


class SendTest(unittest.TestCase):
    def setUp(self):
        hub._sockets.clear()

    def test_send_delivers_and_keeps_socket(self):
        ws = GoodSocket()
        hub.add(ws, "ident-a")
        run(hub.send(ws, "hello"))
        self.assertEqual(ws.sent, ["hello"])
        self.assertEqual(hub.count(), 1)

    def test_send_drops_dead_socket_without_raising(self):
        ws = DeadSocket()
        hub.add(ws, "ident-a")
        run(hub.send(ws, "hello"))          # must not raise
        self.assertEqual(hub.count(), 0)

    def test_broadcast_survives_a_dead_socket(self):
        """The regression: one dead phone must not stop delivery to the others,
        and must not propagate out of broadcast() into the caller's push loop."""
        dead, alive = DeadSocket(), GoodSocket()
        hub.add(dead, "dead")
        hub.add(alive, "alive")
        run(hub.broadcast("payload"))       # must not raise
        self.assertEqual(alive.sent, ["payload"])
        self.assertEqual(hub.online_idents(), {"alive"})


class AnnounceSeqTest(unittest.TestCase):
    def setUp(self):
        hub._announces.clear()

    def test_ids_increase_and_replay_filters_on_them(self):
        with patch.object(hub, "_persist_seq"):
            hub.announce("first")
            hub.announce("second")
        ids = [a["id"] for a in hub._announces]
        self.assertEqual(ids, sorted(ids))
        self.assertLess(ids[0], ids[1])
        self.assertEqual([a["text"] for a in hub.pending_after(ids[0])], ["second"])
        self.assertEqual(hub.pending_after(ids[-1]), [])

    def test_seq_resumes_above_the_persisted_value(self):
        """A restart must not hand out ids the phone has already filtered past."""
        with patch.object(hub, "SEQ_FILE") as f:
            f.read_text.return_value = json.dumps({"next": 838})
            self.assertEqual(hub._load_seq(), 838)

    def test_missing_file_starts_at_one(self):
        with patch.object(hub, "SEQ_FILE") as f:
            f.read_text.side_effect = FileNotFoundError
            self.assertEqual(hub._load_seq(), 1)

    def test_corrupt_file_does_not_reset_to_one(self):
        """Restarting at 1 on a corrupt store would silently reinstate the
        broken-replay bug, so the fallback must be a large value."""
        with patch.object(hub, "SEQ_FILE") as f:
            f.read_text.return_value = "{not json"
            self.assertGreater(hub._load_seq(), 1_000_000)

    def test_announce_persists_the_next_id(self):
        with patch.object(hub, "_persist_seq") as p:
            hub.announce("a line")
        issued = hub._announces[-1]["id"]
        p.assert_called_once_with(issued + 1)


class DedupKeyTest(unittest.TestCase):
    """The push-loop dedup. It was dead code before (state.build() stamps a
    fresh `ts`, so nothing ever compared equal); the risk in fixing it is
    deduping something that genuinely changed and going silent."""

    def test_ts_and_last_seen_alone_compare_equal(self):
        a = snap()
        b = snap(ts=9999.0)
        b["devices"][0]["last_seen"] = 9998.0
        self.assertEqual(util.dedup_key(a), util.dedup_key(b))

    def test_online_flip_is_a_real_change(self):
        """`online` ages out on its own via ONLINE_WINDOW - the phone must see
        it, so it must NOT be stripped alongside last_seen."""
        a = snap()
        b = snap(ts=2000.0)
        b["devices"][0]["online"] = False
        self.assertNotEqual(util.dedup_key(a), util.dedup_key(b))

    def test_cortana_state_change_is_seen(self):
        a = snap()
        b = snap(ts=2000.0)
        b["cortana"]["state"] = "speaking"
        self.assertNotEqual(util.dedup_key(a), util.dedup_key(b))

    def test_board_change_is_seen(self):
        a = snap()
        b = snap(ts=2000.0)
        b["board"]["tasks"] = [{"id": 1, "text": "milk", "done": False}]
        self.assertNotEqual(util.dedup_key(a), util.dedup_key(b))

    def test_device_added_or_removed_is_seen(self):
        a = snap()
        b = snap(ts=2000.0)
        b["devices"] = []
        self.assertNotEqual(util.dedup_key(a), util.dedup_key(b))

    def test_does_not_mutate_the_snapshot_it_is_given(self):
        """The same dict is serialised and sent to phones straight after."""
        a = snap()
        util.dedup_key(a)
        self.assertEqual(a["ts"], 1000.0)
        self.assertEqual(a["devices"][0]["last_seen"], 500.0)

    def test_key_ordering_is_stable(self):
        a = snap()
        b = {k: a[k] for k in reversed(list(a))}
        self.assertEqual(util.dedup_key(a), util.dedup_key(b))

    def test_survives_a_malformed_devices_field(self):
        """state.build() degrades in sections; a broken reader must not take
        the whole push loop down with a TypeError."""
        for bad in (None, "oops", [None, "x"], 42):
            self.assertIsInstance(util.dedup_key(snap(devices=bad)), str)


class PushFloorTest(unittest.TestCase):
    def test_floor_stays_under_the_freshness_window(self):
        """state.cortana_state() marks Cortana live only when the state file is
        under 10s old, and the phone samples that at push time. A floor at or
        above it shows her offline mid-turn."""
        from bridge.settings import PUSH_FLOOR, PUSH_INTERVAL
        self.assertLess(PUSH_FLOOR, 10)
        self.assertGreaterEqual(PUSH_FLOOR, PUSH_INTERVAL)


class PruneTest(unittest.TestCase):
    def setUp(self):
        tasks._tasks.clear()
        tasks._ids = itertools.count(1)

    def _add(self, status):
        tid = next(tasks._ids)
        tasks._tasks[tid] = {"id": tid, "agent": "research", "task": "t",
                             "status": status, "result": "x" * 5000,
                             "started": 0, "finished": 1, "cancel": None}
        return tid

    def test_keeps_only_the_newest_finished(self):
        for _ in range(tasks.FINISHED_KEEP + 15):
            self._add("done")
        tasks._prune()
        self.assertEqual(len(tasks._tasks), tasks.FINISHED_KEEP)
        # the survivors are the NEWEST ones
        self.assertEqual(max(tasks._tasks), tasks.FINISHED_KEEP + 15)

    def test_never_prunes_running_or_queued(self):
        running, queued = self._add("running"), self._add("queued")
        for _ in range(tasks.FINISHED_KEEP + 30):
            self._add("done")
        tasks._prune()
        self.assertIn(running, tasks._tasks)
        self.assertIn(queued, tasks._tasks)
        self.assertEqual({t["id"] for t in tasks.active()}, {running, queued})

    def test_prunes_failed_and_cancelled_too(self):
        for _ in range(tasks.FINISHED_KEEP + 5):
            self._add("failed")
        self._add("cancelled")
        tasks._prune()
        self.assertEqual(len(tasks._tasks), tasks.FINISHED_KEEP)

    def test_status_summary_still_shows_its_last_eight(self):
        """The visible contract: status_summary() only ever listed 8, so the
        cap must not change what the user sees."""
        for _ in range(tasks.FINISHED_KEEP + 40):
            self._add("done")
        tasks._prune()
        self.assertEqual(tasks.status_summary().count("task "), 8)


if __name__ == "__main__":
    unittest.main()
