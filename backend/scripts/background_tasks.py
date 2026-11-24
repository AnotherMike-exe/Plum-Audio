#!/usr/bin/env python3
"""
Plum-Audio — `spawn()`: a fire-and-forget task that cannot vanish and cannot fail in silence.

`asyncio.ensure_future(coro)` with the result thrown away has two faults, and both hide bugs:

  - The event loop holds only a WEAK reference to a task. A task nobody else references can be
    garbage-collected before it finishes, so the work stops part-way with no trace.
  - An exception in such a task is reported only when the task object is collected ("Task exception
    was never retrieved"), which is some arbitrary time later and often never. A transport command
    or a pairing call that failed therefore showed nothing in the log.

`spawn()` keeps a strong reference until the task is done and logs an exception at the moment it
happens, under the task's name. Use it wherever the caller does not await the result. A task the
caller DOES keep and later cancels or awaits (a feeder, a poll loop) does not need it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger("plum.background_tasks")

# Strong references. A task removes itself in its done-callback, so this holds only live work.
_TASKS: set[asyncio.Task] = set()


def _on_done(task: asyncio.Task) -> None:
    _TASKS.discard(task)
    if task.cancelled():
        return  # a cancel is a decision someone made, not a fault
    exc = task.exception()  # also marks the exception retrieved, so asyncio does not report it again
    if exc is not None:
        logger.error("background task %r failed", task.get_name(), exc_info=exc)


def spawn(coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
    """Run `coro` in the background. The task stays referenced, and a failure reaches the log.

    Must be called with a running event loop, like `asyncio.create_task`. Returns the task so a
    caller that wants to cancel it can, but nobody has to keep it.
    """
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _TASKS.add(task)
    task.add_done_callback(_on_done)
    return task


def pending() -> int:
    """How many spawned tasks are still running (for tests and diagnostics)."""
    return len(_TASKS)
