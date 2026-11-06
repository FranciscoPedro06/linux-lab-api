"""What infra/ops/restore-offsite.sh reads from a package and checks on the new stack.

    recovery.py manifest PACKAGE
    recovery.py certificate CADDY_DATA_ARCHIVE SITE
    recovery.py site ADDRESS HTTPS_PORT HTTP_PORT SITE CERTIFICATES

`manifest` prints, one "key value" per line, the facts of a package's manifest the
recovery uses (the package is checked by package.py first). `certificate` prints the
SHA-256 of the certificates Caddy keeps for SITE in its data archive, comma separated
(status 1 when it keeps none). `site` checks what Caddy answers on ADDRESS, as a
browser of SITE would: on HTTP_PORT, a permanent redirect to the same path on
https://SITE; on HTTPS_PORT, with SITE as the TLS server name, a certificate that is
one of CERTIFICATES (SHA-256, comma separated; "-" when the package held none, then
the certificate is not checked), the frontend's entry page with the site's headers,
the API's health and its routes; one line per check, status 1 if any failed. It
changes nothing: GET requests only, without a session.

Standard library only, Python 3.10 or later: it runs on the host, not in an image.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import re
import socket
import ssl
import sys
import tarfile
from pathlib import Path
from typing import Any

ENTRY = b'<div id="root">'
PEM = re.compile(rb"-----BEGIN CERTIFICATE-----\s*(.+?)\s*-----END CERTIFICATE-----", re.DOTALL)


def manifest(package: Path) -> int:
    with tarfile.open(package, "r:") as tar:
        stream = tar.extractfile("manifest.json")
        assert stream is not None
        m: dict[str, Any] = json.load(stream)
    code, content = m["code"], m["code"]["content"]
    facts = {
        "backup_id": m["backup"]["id"],
        "label": m["backup"]["label"],
        "created_at": m["backup"]["created_at"],
        "api_sha": code["api"]["sha"],
        "web_sha": code["web"]["sha"],
        "api_label": code["api"].get("image_revision_label") or "",
        "web_label": code["web"].get("image_revision_label") or "",
        "api_image_id": code["api"]["image_id"],
        "content_sha": content.get("sha") or "",
        "content_source": " ".join(str(content.get("source", "")).split()),
        "caddy_included": "yes" if m["caddy"]["data"] == "included" else "no",
        "caddy_data": m["caddy"]["data"],
    }
    for key, value in facts.items():
        print(f"{key} {value}")
    return 0


def first_certificate(pem: bytes) -> bytes | None:
    match = PEM.search(pem)
    return base64.b64decode(b"".join(match.group(1).split())) if match else None


def certificate(archive: Path, site: str) -> int:
    name = re.escape(site)
    pattern = re.compile(rf"(^|/)caddy/certificates/[^/]+/{name}/{name}\.crt$")
    digests = []
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if member.isreg() and pattern.search(member.name):
                stream = tar.extractfile(member)
                assert stream is not None
                der = first_certificate(stream.read())
                if der is not None:
                    digests.append(hashlib.sha256(der).hexdigest())
    if not digests:
        return 1
    print(",".join(sorted(digests)))
    return 0


class Site:
    """HTTPS to ADDRESS:PORT with SITE as the server name and Host."""

    def __init__(self, address: str, port: int, site: str) -> None:
        self.address, self.port, self.site = address, port, site
        self.context = ssl.create_default_context()
        # The certificate is compared with the restored one instead: on a new host the
        # name does not point here yet, and a test CA is not in the system's store.
        self.context.check_hostname = False
        self.context.verify_mode = ssl.CERT_NONE

    def connect(self) -> ssl.SSLSocket:
        raw = socket.create_connection((self.address, self.port), timeout=10)
        return self.context.wrap_socket(raw, server_hostname=self.site)

    def certificate(self) -> str:
        with self.connect() as tls:
            der = tls.getpeercert(binary_form=True)
        assert der is not None
        return hashlib.sha256(der).hexdigest()

    def get(self, path: str) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPSConnection(self.site, self.port, timeout=30)
        connection.sock = self.connect()
        try:
            connection.request("GET", path, headers={"host": self.site})
            response = connection.getresponse()
            headers = {key.lower(): value for key, value in response.getheaders()}
            return response.status, headers, response.read()
        finally:
            connection.close()


def redirect(address: str, port: int, name: str, path: str) -> tuple[int, str]:
    """Status and Location of a plain HTTP GET of PATH on ADDRESS:PORT as NAME."""
    connection = http.client.HTTPConnection(address, port, timeout=30)
    try:
        connection.request("GET", path, headers={"host": name})
        response = connection.getresponse()
        response.read()
        return response.status, response.getheader("location", "")
    finally:
        connection.close()


def site(address: str, https_port: str, http_port: str, name: str, certificates: str) -> int:
    failures = 0

    def check(condition: bool, what: str, seen: object = "") -> None:
        nonlocal failures
        if condition:
            print(f"ok    {what}")
        else:
            print(f"FAIL  {what}: {seen!r}"[:300])
            failures += 1

    try:
        status, location = redirect(address, int(http_port), name, "/api/health")
        check(
            status in (301, 308) and location == f"https://{name}/api/health",
            "HTTP on the published port 80 redirects to HTTPS, same path",
            (status, location),
        )
    except (OSError, http.client.HTTPException) as error:
        print(f"FAIL  HTTP through {address}:{http_port} as {name}: {error}")
        failures += 1
    target = Site(address, int(https_port), name)
    try:
        served = target.certificate()
        if certificates == "-":
            print(f"note  no certificate for {name} in the package; served {served} (not compared)")
        else:
            check(
                served in certificates.split(","),
                "the certificate served is the restored one",
                served,
            )
        status, headers, body = target.get("/")
        check(status == 200 and ENTRY in body, "the frontend's entry page", status)
        csp = headers.get("content-security-policy", "")
        check("frame-ancestors 'none'" in csp, "the site's Content-Security-Policy", csp)
        check(headers.get("x-content-type-options") == "nosniff", "X-Content-Type-Options")
        check("strict-transport-security" in headers, "Strict-Transport-Security")
        check("server" not in headers, "no Server header", headers.get("server"))
        status, _, body = target.get("/missions/some-mission")
        check(status == 200 and ENTRY in body, "client routes get the entry page", status)
        status, _, body = target.get("/api/health")
        healthy = status == 200 and json.loads(body).get("status") == "ok"
        check(healthy, "the API's health, through Caddy", body)
        status, headers, body = target.get("/api/modules")
        api = status == 401 and headers.get("content-type", "").startswith("application/json")
        check(api, "the API's routes, through Caddy (401 without a session)", status)
        status, _, body = target.get("/openapi.json")
        check(b"openapi" not in body.lower(), "no API documentation through Caddy", status)
    except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as error:
        print(f"FAIL  the site through {address}:{https_port} as {name}: {error}")
        failures += 1
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    match argv:
        case ["manifest", package]:
            return manifest(Path(package))
        case ["certificate", archive, name]:
            return certificate(Path(archive), name)
        case ["site", address, https_port, http_port, name, certificates]:
            return site(address, https_port, http_port, name, certificates)
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
