"""Keep log lines written by worker threads in the function's logs.

The Azure Functions Python worker sends a log line to Application Insights only
when it can tell which run wrote it. Python 3.13 reads that from a ContextVar of
the run; older workers read context.thread_local_storage. Threads started by a
plain ThreadPoolExecutor have neither, so their lines (errors included) were
dropped. Call bind(context) at the start of a function that starts threads, and
create the pools with thread_pool(): each task then runs in a copy of the
caller's context, on a thread that carries the run's invocation id.
"""

import contextvars
from concurrent.futures import ThreadPoolExecutor

# The worker's per-thread storage (the same object for every run). It holds the
# invocation id of the run the current thread belongs to.
_storage = None


def bind(context) -> None:
    """Remember the worker's thread storage, from this run's context."""
    global _storage
    storage = getattr(context, "thread_local_storage", None)
    if storage is not None:
        _storage = storage


def _enter(storage, invocation_id) -> None:
    storage.invocation_id = invocation_id


class _RunPool(ThreadPoolExecutor):
    """Runs each task in a copy of the context of the thread that submitted it."""

    def submit(self, fn, /, *args, **kwargs):
        return super().submit(contextvars.copy_context().run, fn, *args, **kwargs)


def thread_pool(max_workers: int) -> ThreadPoolExecutor:
    """A ThreadPoolExecutor whose threads log as part of the calling run."""
    storage = _storage
    invocation_id = getattr(storage, "invocation_id", None) if storage is not None else None
    if not invocation_id:
        return _RunPool(max_workers=max_workers)
    return _RunPool(max_workers=max_workers, initializer=_enter, initargs=(storage, invocation_id))
