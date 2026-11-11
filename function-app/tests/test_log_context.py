"""Worker threads carry the run's invocation id, so their log lines reach Application Insights."""

import contextvars
import threading
from types import SimpleNamespace

from shared import log_context


def _context(invocation_id):
    storage = threading.local()          # what the worker hands out as context.thread_local_storage
    storage.invocation_id = invocation_id
    return SimpleNamespace(thread_local_storage=storage, invocation_id=invocation_id)


def test_pool_threads_get_the_calling_run_id(monkeypatch):
    monkeypatch.setattr(log_context, "_storage", None)
    context = _context("run-1")
    log_context.bind(context)
    with log_context.thread_pool(3) as pool:
        ids = set(pool.map(lambda _: getattr(context.thread_local_storage, "invocation_id", None), range(6)))
    assert ids == {"run-1"}


def test_nested_pools_keep_the_run_id(monkeypatch):
    monkeypatch.setattr(log_context, "_storage", None)
    context = _context("run-2")
    log_context.bind(context)

    def inner(_):
        with log_context.thread_pool(2) as pool:
            return pool.submit(lambda: context.thread_local_storage.invocation_id).result()

    with log_context.thread_pool(2) as pool:
        assert set(pool.map(inner, range(3))) == {"run-2"}


def test_a_later_run_on_the_same_thread_uses_its_own_id(monkeypatch):
    monkeypatch.setattr(log_context, "_storage", None)
    context = _context("run-1")
    log_context.bind(context)
    context.thread_local_storage.invocation_id = "run-3"   # the worker sets this for each run
    with log_context.thread_pool(1) as pool:
        assert pool.submit(lambda: context.thread_local_storage.invocation_id).result() == "run-3"


def test_pool_tasks_see_the_run_context_variable(monkeypatch):
    # Python 3.13 workers tell the run from a ContextVar, which plain pool threads do not inherit.
    monkeypatch.setattr(log_context, "_storage", None)
    run_id = contextvars.ContextVar("invocation_id", default=None)
    token = run_id.set("run-4")
    try:
        with log_context.thread_pool(3) as pool:
            assert set(pool.map(lambda _: run_id.get(), range(6))) == {"run-4"}
            # Nested pools keep it too.
            inner = pool.submit(lambda: log_context.thread_pool(1).submit(run_id.get).result())
            assert inner.result() == "run-4"
    finally:
        run_id.reset(token)


def test_without_a_context_pools_still_work(monkeypatch):
    monkeypatch.setattr(log_context, "_storage", None)
    log_context.bind(None)
    with log_context.thread_pool(2) as pool:
        assert pool.submit(lambda: 7).result() == 7
