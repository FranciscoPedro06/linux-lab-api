"""Create and remove labs by hand, for working on the terminal locally.

    python -m linuxlab.labs.devlab create
    python -m linuxlab.labs.devlab remove <lab-id>

This is development tooling, not part of the product: labs for students will be
created through lab sessions. Uses LAB_IMAGE and LAB_OCI_RUNTIME.
"""

import argparse
import asyncio
import sys
import uuid

import aiodocker

from linuxlab.config import LabSettings
from linuxlab.labs.access import LAB_ID_PATTERN
from linuxlab.labs.runtime import LabContainerSpec
from linuxlab.labs.runtime.docker import DockerRuntime
from linuxlab.labs.runtime.spec import container_name

WEB_URL = "http://localhost:5173"


async def _create(runtime: DockerRuntime, image: str) -> str:
    lab_id = uuid.uuid4().hex
    info = await runtime.create(LabContainerSpec(lab_id=lab_id, image=image))
    await runtime.start(info.id)
    return lab_id


async def _run(command: str, lab_id: str | None) -> int:
    settings = LabSettings()
    async with aiodocker.Docker() as client:
        runtime = DockerRuntime(client, oci_runtime=settings.lab_oci_runtime)
        await runtime.ping()
        if command == "create":
            lab_id = await _create(runtime, settings.lab_image)
            print(f"lab {lab_id} running under {settings.lab_oci_runtime}")
            print(f"terminal: {WEB_URL}/?lab={lab_id}")
        else:
            assert lab_id is not None
            await runtime.remove(container_name(lab_id))
            print(f"lab {lab_id} removed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m linuxlab.labs.devlab")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create", help="start a new lab")
    remove = commands.add_parser("remove", help="remove a lab")
    remove.add_argument("lab_id")
    args = parser.parse_args(argv)

    lab_id = getattr(args, "lab_id", None)
    if lab_id is not None and not LAB_ID_PATTERN.fullmatch(lab_id):
        parser.error("lab id must be 32 lowercase hex characters")
    return asyncio.run(_run(args.command, lab_id))


if __name__ == "__main__":
    sys.exit(main())
