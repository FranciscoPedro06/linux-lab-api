"""A stand-in for `labctl probe` on a FakeRuntime: answers probe requests from a
described lab state, following the protocol in lab-image/labctl."""

import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from linuxlab.labs.runtime import ExecResult, ExecUser
from linuxlab.labs.runtime.fake import FakeRuntime
from linuxlab.validation.probe import PROBE_COMMAND


@dataclass
class Entry:
    type: str = "file"
    mode: int = 0o644
    user: str = "student"
    group: str = "student"
    content: bytes = b""


@dataclass
class Process:
    comm: str
    args: Sequence[str]
    user: str = "student"
    pid: int = 100


@dataclass
class LabState:
    files: dict[str, Entry] = field(default_factory=dict)
    processes: list[Process] = field(default_factory=list)
    # Paths whose parent directory is a symbolic link.
    linked: set[str] = field(default_factory=set)

    def stat(self, path: str, follow: bool) -> dict[str, Any]:
        if path in self.linked and not follow:
            return {"status": "symlink_in_path"}
        entry = self.files.get(path)
        if entry is None:
            return {"status": "not_found"}
        uid = 1000 if entry.user == "student" else 0
        gid = 1000 if entry.group == "student" else 0
        return {
            "status": "ok",
            "type": entry.type,
            "mode": entry.mode,
            "uid": uid,
            "gid": gid,
            "user": entry.user,
            "group": entry.group,
        }

    def read(self, path: str) -> dict[str, Any]:
        if path in self.linked:
            return {"status": "symlink_in_path"}
        entry = self.files.get(path)
        if entry is None:
            return {"status": "not_found"}
        if entry.type != "file":
            return {"status": "wrong_type"}
        if len(entry.content) > 64 * 1024:
            return {"status": "too_large"}
        return {"status": "ok", "content": base64.b64encode(entry.content).decode()}

    def answer(self, request: dict[str, Any]) -> bytes:
        results = []
        for fact in request["facts"]:
            if fact["op"] == "stat":
                result = self.stat(fact["path"], fact["follow"])
            elif fact["op"] == "read":
                result = self.read(fact["path"])
            else:
                result = {
                    "status": "ok",
                    "processes": [
                        {
                            "pid": process.pid + index,
                            "uid": 1000 if process.user == "student" else 0,
                            "user": process.user,
                            "comm": process.comm,
                            "args": base64.b64encode(
                                b"\0".join(arg.encode() for arg in process.args) + b"\0"
                            ).decode(),
                        }
                        for index, process in enumerate(self.processes)
                    ],
                }
            results.append({**fact, **result})
        return json.dumps({"v": 1, "facts": results}).encode()


def install(runtime: FakeRuntime, state: LabState) -> list[dict[str, Any]]:
    """Answer probe execs on `runtime` from `state`; other execs succeed silently.
    Returns the list the probe requests are appended to."""
    requests: list[dict[str, Any]] = []

    def handler(argv: Sequence[str], user: ExecUser) -> ExecResult:
        if tuple(argv) != PROBE_COMMAND:
            return ExecResult(exit_code=0, stdout=b"", stderr=b"")
        assert user == "root"
        stdin = runtime.exec_calls[-1].stdin
        assert stdin is not None
        request = json.loads(stdin)
        requests.append(request)
        return ExecResult(exit_code=0, stdout=state.answer(request), stderr=b"")

    runtime.exec_handler = handler
    return requests
