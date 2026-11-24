"""spawn(): a fire-and-forget task keeps a strong reference and logs its own failure."""

import asyncio
import gc
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "scripts"))

import background_tasks


def test_spawn_logs_an_exception_when_it_happens(caplog):
    async def boom():
        raise RuntimeError("transport command failed")

    async def run():
        task = background_tasks.spawn(boom(), "avrcp-play")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return task

    with caplog.at_level(logging.ERROR, logger="plum.background_tasks"):
        task = asyncio.run(run())
    assert task.done()
    records = [r for r in caplog.records if "avrcp-play" in r.getMessage()]
    assert records, "the failure must be logged under the task name"
    assert records[0].exc_info is not None and "transport command failed" in str(records[0].exc_info[1])


def test_spawn_keeps_the_task_alive_without_a_caller_reference():
    done = []

    async def work():
        await asyncio.sleep(0.01)
        done.append(True)

    async def run():
        background_tasks.spawn(work(), "work")  # result thrown away on purpose
        assert background_tasks.pending() == 1
        gc.collect()
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert done == [True]
    assert background_tasks.pending() == 0


def test_a_cancelled_task_is_not_reported_as_a_failure(caplog):
    async def run():
        task = background_tasks.spawn(asyncio.sleep(10), "sleeper")
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    with caplog.at_level(logging.ERROR, logger="plum.background_tasks"):
        asyncio.run(run())
    assert not caplog.records
    assert background_tasks.pending() == 0
