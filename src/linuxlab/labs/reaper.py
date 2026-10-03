"""Background task that reconciles labs every 30 seconds, starting at API startup."""

import asyncio
import logging

from linuxlab.labs.lifecycle import Labs
from linuxlab.labs.runtime import LabRuntimeError

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 30


async def run_reaper(labs: Labs, interval: float = INTERVAL_SECONDS) -> None:
    # The first pass runs at startup, with no request in flight: labs left in
    # terminating or failed by a previous process are finished without waiting.
    # It stays a startup pass until one completes.
    startup = True
    while True:
        try:
            await labs.reconcile(startup=startup)
            startup = False
        except LabRuntimeError as error:
            logger.error("lab reconciliation skipped, runtime unavailable: %s", error)
        except Exception:
            logger.exception("lab reconciliation failed")
        await asyncio.sleep(interval)
