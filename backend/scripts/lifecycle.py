#!/usr/bin/env python3
"""
Process shutdown plumbing shared by the two audio processes.

Both `sendspin_server` and `sendspin_player` end their `main()` in `await stop.wait()` with a
carefully written `finally:` that stops the source managers, kills the source daemons, closes the
Avahi advertisement and releases the PortAudio stream.

None of it ever ran. Nothing installed a SIGTERM handler, and Python's default disposition for
SIGTERM terminates the interpreter immediately — no exception, no `finally`. Supervisord stops
these programs with `stopsignal=TERM`, so every `supervisorctl restart` orphaned every
shairport-sync, go-librespot, bluealsa, obexd and private dbus-daemon the managers had spawned
(they are started with `start_new_session=True`, so they do not even take the process group's
signal). `_kill_stale_daemons` sweeps them at the NEXT start, which is a mitigation, not a
shutdown — and it is why a restart looked clean while leaving the previous generation running.

SIGINT is included so an interactive run on the dev rig takes the same path as the container.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

logger = logging.getLogger(__name__)


def install_shutdown_handlers() -> asyncio.Event:
    """Return an Event that is set on SIGTERM/SIGINT.

    `add_signal_handler` is the asyncio-safe form: it wakes the loop rather than running the
    callback on whatever stack the signal interrupted, so the `finally:` unwinds normally.
    NotImplementedError is suppressed for platforms without it (Windows) — the process then behaves
    exactly as it did before, rather than failing to start.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(signame: str) -> None:
        logger.info("%s received — shutting down", signame)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_stop, sig.name)

    return stop
