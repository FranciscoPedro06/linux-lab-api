"""Client side of the end-to-end checks of the production stack (infra/tests/e2e.sh).

Runs in a container on the stack's public network, so its address is the one Caddy
sees, and talks to the site only through Caddy. Standard library and websockets
only: it runs in the release's API image. Each command prints one line per check and
exits 1 if any failed.

    python e2e.py logins COUNT EXPECTED_STATUS [FORWARDED_FOR]
    python e2e.py connect HOST PORT
    python e2e.py site
    python e2e.py flow EMAIL OTHER_EMAIL
    python e2e.py restart EMAIL
"""

import contextlib
import http.client
import json
import os
import socket
import ssl
import sys
import time
import uuid
from typing import Any

from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import ClientConnection, connect

HOST = os.environ.get("E2E_HOST", "")
ORIGIN = os.environ.get("E2E_ORIGIN", "")
INVITE = os.environ.get("E2E_INVITE", "")
CA = os.environ.get("E2E_CA", "/e2e/root.crt")
PASSWORD = "synthetic password 01"
COOKIE = "__Host-sid"

OPEN = contextlib.ExitStack()

failures: list[str] = []


def expect(condition: bool, what: str, seen: Any = None) -> None:
    if condition:
        print(f"ok    {what}", flush=True)
    else:
        print(f"FAIL  {what}: {seen!r}", flush=True)
        failures.append(what)


def finish() -> None:
    sys.exit(1 if failures else 0)


def tls() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=CA)


class Site:
    """HTTPS to the site through Caddy, keeping the session cookie like a browser."""

    def __init__(self) -> None:
        self.cookie: str | None = None

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPSConnection(HOST, 443, context=tls(), timeout=150)
        sent = {"origin": ORIGIN, **(headers or {})}
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            sent["content-type"] = "application/json"
        if self.cookie:
            sent["cookie"] = f"{COOKIE}={self.cookie}"
        connection.request(method, path, body=data, headers=sent)
        response = connection.getresponse()
        payload = response.read()
        received = {name.lower(): value for name, value in response.getheaders()}
        for name, value in response.getheaders():
            if name.lower() == "set-cookie" and value.startswith(f"{COOKIE}="):
                self.cookie = value.split(";", 1)[0].split("=", 1)[1] or None
        connection.close()
        return response.status, received, payload

    def json(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        status, _, payload = self.request(method, path, body)
        try:
            return status, json.loads(payload)
        except ValueError:
            return status, payload[:200]

    def terminal(
        self, lab_id: str, *, origin: str = ORIGIN, cookie: bool = True
    ) -> ClientConnection:
        headers = {"cookie": f"{COOKIE}={self.cookie}"} if cookie and self.cookie else {}
        return connect(
            f"wss://{HOST}/ws/labs/{lab_id}/terminal",
            ssl=tls(),
            origin=origin,  # type: ignore[arg-type]
            additional_headers=headers,
            open_timeout=10,
        )

    def account(self, email: str) -> None:
        body = {"email": email, "password": PASSWORD, "display_name": "E2E", "invite_code": INVITE}
        status, _ = self.json("POST", "/api/auth/signup", body)
        if status == 409:
            status, _ = self.json("POST", "/api/auth/login", {"email": email, "password": PASSWORD})
        expect(status in (200, 201), f"signed in as {email}", status)


def read_until(terminal: ClientConnection, marker: bytes, seconds: float = 20) -> bytes:
    seen = b""
    deadline = time.monotonic() + seconds
    while marker not in seen:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError(seen[-300:])
        message = terminal.recv(timeout=left)
        if isinstance(message, bytes):
            seen += message
    return seen


def open_shell(site: Site, lab_id: str) -> ClientConnection:
    """A terminal kept open across steps; closed when the command ends."""
    terminal = OPEN.enter_context(site.terminal(lab_id))
    terminal.send(json.dumps({"type": "init", "cols": 100, "rows": 30}))
    ready = terminal.recv(timeout=15)
    expect(ready == json.dumps({"type": "ready"}), "terminal ready over wss through Caddy", ready)
    read_until(terminal, b"$ ")
    return terminal


def run_in_shell(terminal: ClientConnection, line: str) -> bytes:
    # The marker is split in the typed line, so only the command's output matches it.
    marker = f"done-{uuid.uuid4().hex[:8]}"
    terminal.send(f"{line}; echo {marker[:4]}''{marker[4:]}\r".encode())
    return read_until(terminal, marker.encode())


def close_code(connection: ClientConnection, seconds: float = 30) -> int | None:
    try:
        while True:
            connection.recv(timeout=seconds)
    except ConnectionClosed as closed:
        return closed.rcvd.code if closed.rcvd else 1006
    except TimeoutError:
        return None


def handshake_status(site: Site, lab_id: str, origin: str) -> int | None:
    try:
        with site.terminal(lab_id, origin=origin):
            return 101
    except InvalidStatus as error:
        return error.response.status_code


# Commands


def logins(count: str, expected: str, forwarded_for: str | None = None) -> None:
    """Failed logins from this container's address, optionally with a forged header."""
    site = Site()
    headers = {"x-forwarded-for": forwarded_for} if forwarded_for else {}
    body = {"email": "nobody@example.com", "password": "synthetic wrong 01"}
    statuses = [
        site.request("POST", "/api/auth/login", body, headers)[0] for _ in range(int(count))
    ]
    label = f"{count} logins answered {expected}" + (
        f" (X-Forwarded-For: {forwarded_for})" if forwarded_for else ""
    )
    expect(statuses == [int(expected)] * int(count), label, statuses)
    finish()


def can_connect(host: str, port: str) -> None:
    """Exit 0 if a TCP connection to host:port opens within 3 seconds."""
    try:
        with socket.create_connection((host, int(port)), timeout=3):
            print(f"connected to {host}:{port}")
            sys.exit(0)
    except OSError as error:
        print(f"no connection to {host}:{port}: {error}")
        sys.exit(1)


def site_checks() -> None:
    site = Site()
    status, headers, body = site.request("GET", "/")
    expect(status == 200 and b'<div id="root">' in body, "entry page served", status)
    csp = headers.get("content-security-policy", "")
    expect("default-src 'self'" in csp and "frame-ancestors 'none'" in csp, "CSP header", csp)
    expect(headers.get("x-content-type-options") == "nosniff", "nosniff header")
    expect("strict-transport-security" in headers, "HSTS header")
    expect("server" not in headers, "no Server header", headers.get("server"))
    expect(
        headers.get("cache-control") == "no-cache",
        "entry page revalidated",
        headers.get("cache-control"),
    )
    asset = body.split(b'src="', 1)[1].split(b'"', 1)[0].decode()
    status, headers, _ = site.request("GET", asset)
    expect(
        status == 200 and "immutable" in headers.get("cache-control", ""),
        f"hashed asset cached for a year ({asset})",
        (status, headers.get("cache-control")),
    )
    status, _, body = site.request("GET", "/labs/some-client-route")
    expect(status == 200 and b'<div id="root">' in body, "client routes get the entry page", status)
    for path in ("/docs", "/openapi.json", "/redoc"):
        status, _, body = site.request("GET", path)
        expect(
            b"openapi" not in body.lower() and b"swagger" not in body.lower(),
            f"{path} does not reach the API",
            body[:80],
        )
    status, health = site.json("GET", "/api/health")
    expect(status == 200 and health.get("status") == "ok", "health through Caddy", health)
    big = {"email": "x" * 70_000, "password": "y"}
    status, _ = site.json("POST", "/api/auth/login", big)
    expect(status == 413, "request body over 64 KB refused by Caddy", status)
    plain = http.client.HTTPConnection(HOST, 80, timeout=10)
    plain.request("GET", "/api/health")
    response = plain.getresponse()
    expect(
        response.status in (301, 308) and response.getheader("location", "").startswith("https://"),
        "HTTP redirects to HTTPS",
        (response.status, response.getheader("location")),
    )
    finish()


def flow(email: str, other_email: str) -> None:
    """A student's whole session, and what another user and other origins cannot do."""
    site = Site()
    site.account(email)

    status, lab = site.json("POST", "/api/labs", {"mission_slug": "sample-file"})
    expect(status == 201 and lab.get("status") == "ready", "lab created", (status, lab))
    lab_id = lab["id"]
    print(f"lab {lab_id}")

    terminal = open_shell(site, lab_id)
    output = run_in_shell(terminal, "chmod 600 ~/sample.sh && stat -c %a ~/sample.sh")
    expect(b"600" in output, "command ran in the lab", output[-200:])
    run_in_shell(terminal, "echo e2e-history-line")

    status, result = site.json("POST", f"/api/labs/{lab_id}/validate", {})
    expect(
        status == 200 and result.get("outcome") == "passed", "validation passed", (status, result)
    )

    commands: list[str] = []
    for _ in range(20):
        status, history = site.json("GET", f"/api/labs/{lab_id}/history")
        commands = (
            [entry["command"] for entry in history.get("entries", [])] if status == 200 else []
        )
        if any("e2e-history-line" in command for command in commands):
            break
        time.sleep(0.5)
    expect(
        any("chmod 600" in command for command in commands)
        and any("e2e-history-line" in command for command in commands),
        "history recorded",
        commands,
    )

    expect(
        handshake_status(site, lab_id, "https://evil.example") == 403,
        "terminal refused for another origin",
    )
    with site.terminal(lab_id, cookie=False) as anonymous:
        expect(close_code(anonymous) == 4401, "terminal without a session closed with 4401")

    other = Site()
    other.account(other_email)
    status, _ = other.json("GET", f"/api/labs/{lab_id}")
    expect(status == 404, "another user cannot see the lab", status)
    with other.terminal(lab_id) as foreign:
        expect(close_code(foreign) == 4404, "another user's terminal closed with 4404")
    status, _ = other.json("POST", f"/api/labs/{lab_id}/validate", {})
    expect(status == 404, "another user cannot validate the lab", status)

    started = time.monotonic()
    status, reset = site.json(
        "POST", f"/api/labs/{lab_id}/replace", {"mission_slug": "sample-file"}
    )
    expect(
        status == 201 and reset.get("id") != lab_id and reset.get("status") == "ready",
        f"reset through Caddy ({time.monotonic() - started:.1f} s)",
        (status, reset),
    )
    expect(close_code(terminal) == 4410, "old lab's terminal closed with 4410")
    reset_id = reset["id"]
    print(f"lab {reset_id}")
    status, history = site.json("GET", f"/api/labs/{reset_id}/history")
    expect(
        status == 200 and history.get("entries") == [], "reset lab starts with no history", history
    )

    status, switched = site.json(
        "POST", f"/api/labs/{reset_id}/replace", {"mission_slug": "sample-answer"}
    )
    expect(
        status == 201 and switched.get("status") == "ready", "mission switch", (status, switched)
    )
    switched_id = switched["id"]
    print(f"lab {switched_id}")
    terminal = open_shell(site, switched_id)
    expect(b"ok" in run_in_shell(terminal, "echo ok"), "terminal in the switched lab")

    status, ended = site.json("DELETE", f"/api/labs/{switched_id}", {})
    expect(
        status == 200 and ended.get("status") in ("terminated", "terminating"),
        "lab ended",
        (status, ended),
    )
    expect(close_code(terminal) == 4410, "ended lab's terminal closed with 4410")

    status, _, _ = site.request("POST", "/api/auth/logout", {})
    expect(status == 204, "logout", status)
    status, _ = site.json("GET", "/api/auth/me")
    expect(status == 401, "session gone after logout", status)
    other.request("POST", "/api/auth/logout", {})
    finish()


def restart(email: str) -> None:
    """Holds a terminal while e2e.sh restarts the API, then reconnects like the web."""
    site = Site()
    site.account(email)
    status, lab = site.json("POST", "/api/labs", {"mission_slug": "sample-file"})
    expect(status == 201, "lab created", (status, lab))
    terminal = open_shell(site, lab["id"])
    print("holding", flush=True)
    code = close_code(terminal, seconds=180)
    expect(code in (1001, 1012), "terminal closed as a server restart", code)

    reconnected = False
    for _ in range(30):
        try:
            status, current = site.json("GET", f"/api/labs/{lab['id']}")
            if status == 200 and current.get("status") == "ready":
                terminal = open_shell(site, lab["id"])
                reconnected = b"back" in run_in_shell(terminal, "echo back")
                break
        except (OSError, ConnectionClosed, http.client.HTTPException, TimeoutError):
            pass
        time.sleep(2)
    expect(reconnected, "lab kept and terminal reopened after the restart")
    site.json("DELETE", f"/api/labs/{lab['id']}", {})
    finish()


if __name__ == "__main__":
    command, *arguments = sys.argv[1:]
    commands = {
        "logins": logins,
        "connect": can_connect,
        "site": site_checks,
        "flow": flow,
        "restart": restart,
    }
    with OPEN:
        commands[command](*arguments)
