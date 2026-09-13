"""Synthetic local HTTP boundary for the runnable example and native tests."""

from __future__ import annotations

import copy
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from .transport import ORIGIN

TARGET = "https://selected-agent.example.net/mcp?public=yes"
ISSUER = "did:key:z6MkeXBLjYiSvqnhFb6D7sHm8yKm4jV45wwBFRaatf1cfZ76"
SUBJECT = "did:key:z6Mkeb6dsrBTX95vPgLiZAcgRr6XJthFm1czoqFEx34DQtRo"


def observed():
    value = json.loads((Path(__file__).parent / "fixtures/preflight.raw").read_text())
    value["target"] = TARGET
    return value


def public_passport():
    now = datetime.now(timezone.utc)
    return {
        "@context": ["https://www.w3.org/ns/credentials/v2"],
        "type": ["VerifiableCredential", "AgentGuildPassport"],
        "issuer": ISSUER,
        "credentialSubject": {"id": SUBJECT, "public_claim": {"note": "Synthetic fixture only", "unknown": None}},
        "validFrom": (now - timedelta(seconds=1)).isoformat(),
        "validUntil": (now + timedelta(minutes=5)).isoformat(),
        "proof": {
            "type": "DataIntegrityProof",
            "cryptosuite": "eddsa-jcs-2022",
            "proofPurpose": "assertionMethod",
            "verificationMethod": ISSUER + "#" + ISSUER[8:],
            "proofValue": "z4VZdodJgBy6dxMgm45zusmRzrPvKtiumu5YrK9RLPJADpzeJzgebxHsoQD4B58FCFS6aGUufKZka56xFiBGpB94",
        },
    }


def passport_request():
    # Independent fixture constants, not copied out of the received credential at the call site.
    return {"credential": public_passport(), "expected_issuer_did": ISSUER, "expected_subject_did": SUBJECT}


def verification(*, valid=True, guild_issued=True):
    return {"valid": valid, "guild_issued": guild_issued, "issuer": ISSUER, "subject_did": SUBJECT}


class LocalFixture:
    """A real loopback server; only requests to the fixed Guild origin are mapped here."""

    def __init__(self):
        self.requests = []
        self.reply(observed())
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                body = self.rfile.read(int(self.headers.get("content-length", "0")))
                fixture.requests.append({"method": self.command, "path": self.path, "body": body.decode("utf-8")})
                response = copy.copy(fixture.response)
                time.sleep(response["delay"])
                self.send_response(response["status"])
                for key, value in response["headers"].items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(response["body"])))
                self.end_headers()
                try:
                    self.wfile.write(response["body"])
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    def reply(self, value, *, status=200, headers=None, delay=0):
        body = value if isinstance(value, bytes) else json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.response = {
            "body": body,
            "status": status,
            "headers": {"Content-Type": "application/json", **(headers or {})},
            "delay": delay,
        }

    def transport(self):
        local = f"http://127.0.0.1:{self.server.server_port}"

        class MappedGuildHTTP(httpx.AsyncBaseTransport):
            def __init__(self):
                self.inner = httpx.AsyncHTTPTransport(retries=0, trust_env=False)

            async def handle_async_request(self, request):
                assert str(request.url).split("/", 3)[:3] == ORIGIN.split("/")
                assert request.url.path in {"/preflight", "/credentials/verify"}
                mapped = httpx.Request(
                    request.method,
                    local + request.url.raw_path.decode("ascii"),
                    headers=request.headers,
                    content=await request.aread(),
                    extensions=request.extensions,
                )
                response = await self.inner.handle_async_request(mapped)
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    stream=response.stream,
                    extensions=response.extensions,
                    request=request,
                )

            async def aclose(self):
                await self.inner.aclose()

        return MappedGuildHTTP()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
