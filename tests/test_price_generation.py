import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from signal_desk.generation_cache import generation_cache


@pytest.mark.parametrize("change_revision", [True, False])
def test_inflight_old_input_cannot_repopulate_current_cache(change_revision):
    entered, release = Event(), Event()
    state = {"revision": 0, "price": 100, "calls": 0}

    @generation_cache(lambda: state["revision"])
    def compute():
        state["calls"] += 1
        price = state["price"]
        if state["calls"] == 1:
            entered.set()
            assert release.wait(3)
        return price

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(compute)
        assert entered.wait(3)
        state["price"] = 120
        if change_revision:
            state["revision"] += 1
        compute.cache_clear()
        release.set()
        assert pending.result(timeout=3) == 120
    assert compute() == 120 and state["calls"] == 2


def test_new_price_revision_refreshes_without_manual_clear():
    state = {"revision": 0, "price": 100}

    @generation_cache(lambda: state["revision"])
    def compute():
        return state["price"]

    assert compute() == 100
    state.update(revision=1, price=120)
    assert compute() == 120


def test_bot_waits_for_first_quote_pass(monkeypatch):
    from signal_desk import api

    async def scenario():
        first = asyncio.Event()
        calls = []
        monkeypatch.setattr(api, "_loop_owner_is_me", lambda: True)

        def tick():
            calls.append(first.is_set())
            raise asyncio.CancelledError

        monkeypatch.setattr(api, "_bot_loop_iteration", tick)
        task = asyncio.create_task(api._bot_loop(first))
        await asyncio.sleep(0)
        assert calls == [] and not task.done()
        first.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == [True]

    asyncio.run(scenario())


@pytest.mark.parametrize("failed", [False, True])
def test_quote_pass_releases_startup_only_after_attempt(monkeypatch, failed):
    from signal_desk import api

    async def scenario():
        first = asyncio.Event()
        sleeps = 0

        async def sleep(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 1:
                raise asyncio.CancelledError

        async def to_thread(fn, *args):
            return fn(*args)

        def tick():
            assert not first.is_set()
            if failed:
                raise RuntimeError("provider failed")

        monkeypatch.setattr(api.asyncio, "sleep", sleep)
        monkeypatch.setattr(api.asyncio, "to_thread", to_thread)
        monkeypatch.setattr(api, "_own_loop_tick", lambda: True)
        monkeypatch.setattr(api, "_quote_loop_iteration", tick)
        with pytest.raises(asyncio.CancelledError):
            await api._quote_loop(first)
        assert first.is_set()

    asyncio.run(scenario())
