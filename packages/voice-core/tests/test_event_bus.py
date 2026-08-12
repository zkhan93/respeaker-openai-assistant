"""Tests for the ordered, per-subscriber :class:`EventBus`.

These pin the guarantees the dispatch rewrite introduced (replacing the
old thread-per-callback delivery, which had no ordering):

* per-subscriber FIFO ordering across every event type it subscribes to;
* isolation (a slow subscriber never stalls another);
* one worker per ordering domain, reaped when its last subscription goes;
* re-entrant publish without deadlock;
* ``shutdown()`` drains queued work and is idempotent.

Determinism note: ``shutdown()`` appends a stop sentinel *after* every
already-queued item and joins the worker, so "publish everything, then
``shutdown()``, then assert" needs no sleeps for events queued before the
call. Concurrency and re-entrant cases synchronize on ``threading.Event``
with generous timeouts instead of timing assumptions.
"""

import threading

import pytest

from voice_core.bus.event_bus import EventBus


@pytest.fixture
def bus():
    b = EventBus()
    yield b
    b.shutdown()  # idempotent — safe even if the test already called it


# --- helpers ---------------------------------------------------------------


class Recorder:
    """Component with two bound-method handlers (grouped by ``__self__``)."""

    def __init__(self):
        self.seen = []

    def on_a(self, ev):
        self.seen.append(("a", ev))

    def on_b(self, ev):
        self.seen.append(("b", ev))


# --- ordering (the core fix) ----------------------------------------------


def test_delivers_all_events(bus):
    rec = Recorder()
    bus.subscribe("a", rec.on_a)
    bus.publish("a", 1)
    bus.publish("a", 2)
    bus.shutdown()
    assert rec.seen == [("a", 1), ("a", 2)]


def test_preserves_publish_order_across_event_types(bus):
    """A subscriber sees events in publish order across ALL its event types.

    This is the guarantee DuckController/ConversationManager rely on and
    that the old thread-per-callback dispatch violated.
    """
    rec = Recorder()
    bus.subscribe("a", rec.on_a)
    bus.subscribe("b", rec.on_b)

    expected = []
    for i in range(500):
        bus.publish("a", i)
        expected.append(("a", i))
        bus.publish("b", i)
        expected.append(("b", i))
    bus.shutdown()

    assert rec.seen == expected


def test_speaking_started_always_precedes_stopped(bus):
    """started/stopped published back-to-back must never be reordered."""
    log = {"count": 0, "went_negative": False}

    class Consumer:
        def on_started(self, _ev):
            log["count"] += 1

        def on_stopped(self, _ev):
            log["count"] -= 1
            if log["count"] < 0:
                log["went_negative"] = True

    c = Consumer()
    bus.subscribe("speaking_started", c.on_started)
    bus.subscribe("speaking_stopped", c.on_stopped)
    for i in range(1000):
        bus.publish("speaking_started", i)
        bus.publish("speaking_stopped", i)
    bus.shutdown()

    assert not log["went_negative"]
    assert log["count"] == 0


# --- isolation -------------------------------------------------------------


def test_slow_subscriber_does_not_block_others(bus):
    gate = threading.Event()
    fast_ran = threading.Event()

    class Slow:
        def handle(self, _ev):
            gate.wait(5.0)  # block until the test releases us

    class Fast:
        def handle(self, _ev):
            fast_ran.set()

    slow, fast = Slow(), Fast()
    bus.subscribe("e", slow.handle)
    bus.subscribe("e", fast.handle)

    bus.publish("e", 1)
    # If delivery were serialized across subscribers, fast would wait
    # behind slow's 5s block and this would time out.
    assert fast_ran.wait(2.0), "fast subscriber was blocked behind the slow one"
    gate.set()


# --- ordering-domain grouping & worker lifecycle ---------------------------


def test_bound_methods_of_one_instance_share_one_worker(bus):
    rec = Recorder()
    bus.subscribe("a", rec.on_a)
    bus.subscribe("b", rec.on_b)
    assert len(bus._workers) == 1  # grouped by __self__


def test_plain_functions_get_independent_workers(bus):
    def h1(_ev):
        pass

    def h2(_ev):
        pass

    bus.subscribe("a", h1)
    bus.subscribe("b", h2)
    assert len(bus._workers) == 2


def test_order_key_groups_closures(bus):
    """Closures (no __self__) can be grouped explicitly — DuckController's case."""
    domain = object()

    def started(_ev):
        pass

    def ended(_ev):
        pass

    bus.subscribe("conversation_turn_started", started, order_key=domain)
    bus.subscribe("conversation_turn_ended", ended, order_key=domain)
    assert len(bus._workers) == 1


def test_worker_reaped_when_last_subscription_removed(bus):
    rec = Recorder()
    bus.subscribe("a", rec.on_a)
    bus.subscribe("b", rec.on_b)
    assert len(bus._workers) == 1

    bus.unsubscribe("a", rec.on_a)
    assert len(bus._workers) == 1  # on_b still holds the shared worker

    bus.unsubscribe("b", rec.on_b)
    assert len(bus._workers) == 0  # reaped


def test_no_worker_or_thread_leak_across_cycles(bus):
    base_threads = threading.active_count()
    for _ in range(25):
        rec = Recorder()
        bus.subscribe("a", rec.on_a)
        bus.subscribe("b", rec.on_b)
        bus.unsubscribe("a", rec.on_a)
        bus.unsubscribe("b", rec.on_b)
    assert len(bus._workers) == 0
    # unsubscribe joins the reaped worker before returning, so the count
    # is settled with no sleep needed.
    assert threading.active_count() == base_threads


# --- interruption refcount (DuckController 1->2->1) ------------------------


def test_interruption_refcount_never_unducks_midstream(bus):
    """Grouped closures preserve the 1->2->1 count with no unduck->reduck blip."""
    state = {"count": 0, "was_zero": False, "blip": False}

    def note():
        if state["count"] == 0:
            state["was_zero"] = True
        elif state["count"] > 0 and state["was_zero"]:
            state["blip"] = True  # unducked then re-ducked — the bug

    domain = object()

    def on_started(_ev):
        state["count"] += 1
        note()

    def on_ended(_ev):
        state["count"] -= 1
        note()

    bus.subscribe("conversation_turn_started", on_started, order_key=domain)
    bus.subscribe("conversation_turn_ended", on_ended, order_key=domain)

    bus.publish("conversation_turn_started", 0)  # first turn: 0->1
    for _ in range(50):
        # interruption: new turn claims BEFORE old turn releases -> 1->2->1
        bus.publish("conversation_turn_started", 1)
        bus.publish("conversation_turn_ended", 0)
    bus.publish("conversation_turn_ended", 1)  # final: 1->0
    bus.shutdown()

    assert not state["blip"]
    assert state["count"] == 0


# --- re-entrancy & error handling ------------------------------------------


def test_reentrant_publish_from_handler(bus):
    """A handler may publish back into the bus (ConversationManager does)."""
    got = []
    done = threading.Event()

    class Producer:
        def on_in(self, ev):
            bus.publish("out", ev * 10)

    class Consumer:
        def on_out(self, ev):
            got.append(ev)
            done.set()

    bus.subscribe("in", Producer().on_in)
    bus.subscribe("out", Consumer().on_out)

    bus.publish("in", 7)
    assert done.wait(2.0), "re-entrant publish never delivered (deadlock?)"
    assert got == [70]


def test_callback_exception_does_not_kill_worker(bus):
    """One handler raising must not stop its worker or affect siblings."""
    seen = []

    class Flaky:
        def handle(self, ev):
            if ev == 1:
                raise ValueError("boom")
            seen.append(ev)

    bus.subscribe("e", Flaky().handle)
    bus.publish("e", 1)  # raises inside the worker — must be swallowed
    bus.publish("e", 2)  # worker must still be alive to process this
    bus.shutdown()

    assert seen == [2]


def test_exception_in_one_subscriber_isolated_from_others(bus):
    other_seen = []

    class Boom:
        def handle(self, _ev):
            raise RuntimeError("nope")

    class Good:
        def handle(self, ev):
            other_seen.append(ev)

    bus.subscribe("e", Boom().handle)
    bus.subscribe("e", Good().handle)
    bus.publish("e", 42)
    bus.shutdown()

    assert other_seen == [42]


# --- shutdown --------------------------------------------------------------


def test_shutdown_drains_pending_events():
    bus = EventBus()
    drained = []

    class Drainer:
        def handle(self, ev):
            drained.append(ev)

    bus.subscribe("z", Drainer().handle)
    for i in range(50):
        bus.publish("z", i)
    bus.shutdown()  # must block until the queue is fully drained
    assert drained == list(range(50))


def test_shutdown_is_idempotent(bus):
    bus.subscribe("e", lambda ev: None)
    bus.shutdown()
    bus.shutdown()  # second call must not raise


def test_subscribe_after_shutdown_still_works():
    bus = EventBus()
    bus.shutdown()
    seen = []
    bus.subscribe("e", lambda ev: seen.append(ev))
    bus.publish("e", 1)
    bus.shutdown()
    assert seen == [1]


# --- misc public API -------------------------------------------------------


def test_get_subscriber_count(bus):
    assert bus.get_subscriber_count("e") == 0
    cb1, cb2 = (lambda ev: None), (lambda ev: None)
    bus.subscribe("e", cb1)
    bus.subscribe("e", cb2)
    assert bus.get_subscriber_count("e") == 2
    bus.unsubscribe("e", cb1)
    assert bus.get_subscriber_count("e") == 1


def test_publish_with_no_subscribers_is_noop(bus):
    bus.publish("nobody_home", object())  # must not raise


def test_unsubscribe_unknown_is_safe(bus):
    bus.unsubscribe("never_subscribed", lambda ev: None)  # must not raise
    rec = Recorder()
    bus.subscribe("a", rec.on_a)
    bus.unsubscribe("a", lambda ev: None)  # wrong callback — no-op
    assert bus.get_subscriber_count("a") == 1
