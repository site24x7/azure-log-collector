"""Keep log lines written by worker threads in the function's logs.

The Azure Functions Python worker sends a log line to Application Insights only
when the thread that wrote it carries the run's invocation id. Threads started
by a ThreadPoolExecutor do not, so their lines (errors included) were dropped.
Call bind(context) at the start of a function that starts threads, and create
the pools with thread_pool().
"""

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


def thread_pool(max_workers: int) -> ThreadPoolExecutor:
    """A ThreadPoolExecutor whose threads log as part of the calling run."""
    storage = _storage
    invocation_id = getattr(storage, "invocation_id", None) if storage is not None else None
    if not invocation_id:
        return ThreadPoolExecutor(max_workers=max_workers)
    return ThreadPoolExecutor(max_workers=max_workers, initializer=_enter,
                              initargs=(storage, invocation_id))
