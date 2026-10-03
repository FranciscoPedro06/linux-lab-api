import asyncio
from typing import cast

from linuxlab.labs.lifecycle import Labs
from linuxlab.labs.reaper import run_reaper
from linuxlab.labs.runtime import LabRuntimeError


class FlakyLabs:
    def __init__(self) -> None:
        self.passes: list[bool] = []

    async def reconcile(self, *, startup: bool = False) -> None:
        self.passes.append(startup)
        if len(self.passes) == 1:
            raise LabRuntimeError("Docker is not reachable")
        if len(self.passes) == 2:
            raise RuntimeError("unexpected")


async def test_reaper_keeps_running_and_repeats_the_startup_pass_until_it_completes() -> None:
    labs = FlakyLabs()
    task = asyncio.create_task(run_reaper(cast(Labs, labs), interval=0))
    async with asyncio.timeout(5):
        while len(labs.passes) < 5:
            await asyncio.sleep(0)
    task.cancel()

    assert labs.passes[:5] == [True, True, True, False, False]
