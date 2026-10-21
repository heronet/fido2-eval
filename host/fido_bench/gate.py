"""Verify CTAP assertions and consume approvals before dispatch."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fido2 import cbor
from fido2.cose import CoseKey
from fido2.webauthn import AuthenticatorData


class Rejected(ValueError):
    pass


def canonical(value: dict) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


@dataclass(frozen=True)
class Binding:
    node_id: str
    rp_id: str
    credential_id: bytes
    cose_key: bytes

    def save(self, path: str | Path) -> None:
        with open(path, "x", encoding="utf-8") as f:
            json.dump(
                {
                    "schema": 1,
                    "node_id": self.node_id,
                    "rp_id": self.rp_id,
                    "credential_id": base64.b64encode(self.credential_id).decode(),
                    "cose_key": base64.b64encode(self.cose_key).decode(),
                },
                f,
                indent=2,
            )

    @classmethod
    def load(cls, path: str | Path) -> "Binding":
        d = json.loads(Path(path).read_text())
        if d.get("schema") != 1:
            raise ValueError("Unsupported binding schema")
        return cls(
            d["node_id"],
            d["rp_id"],
            base64.b64decode(d["credential_id"], validate=True),
            base64.b64decode(d["cose_key"], validate=True),
        )


@dataclass(frozen=True)
class Evidence:
    credential_id: bytes
    auth_data: bytes
    signature: bytes


@dataclass(frozen=True)
class Request:
    encoded: bytes

    @property
    def payload(self) -> dict:
        # Decode a fresh copy for each access.
        return json.loads(self.encoded)

    @property
    def digest(self) -> bytes:
        return hashlib.sha256(self.encoded).digest()

    @property
    def nonce(self) -> str:
        return self.payload["nonce"]


def verify(binding: Binding, request: Request, evidence: Evidence) -> None:
    try:
        p = request.payload
    except (ValueError, TypeError) as e:
        raise Rejected("Malformed request") from e
    if not isinstance(p, dict) or set(p) != {
        "protocol",
        "node_id",
        "operation",
        "parameters",
        "nonce",
    }:
        raise Rejected("Invalid request schema")
    nonce = p["nonce"]
    if (
        not isinstance(nonce, str)
        or len(nonce) != 64
        or any(c not in "0123456789abcdef" for c in nonce)
    ):
        raise Rejected("Invalid nonce")
    if p.get("protocol") != "fido-node-maintenance-v1" or p.get("node_id") != binding.node_id:
        raise Rejected("Wrong protocol or node binding")
    if p.get("operation") != "set_alarm_threshold":
        raise Rejected("Unknown operation")
    params = p.get("parameters", {})
    if not isinstance(params, dict):
        raise Rejected("Invalid parameters")
    value = params.get("threshold_mdegc")
    if (
        set(params) != {"threshold_mdegc"}
        or type(value) is not int
        or not -40000 <= value <= 125000
    ):
        raise Rejected("Invalid parameters")
    if evidence.credential_id != binding.credential_id:
        raise Rejected("Wrong credential")
    try:
        data = AuthenticatorData(evidence.auth_data)
        if data.rp_id_hash != hashlib.sha256(binding.rp_id.encode()).digest():
            raise Rejected("Wrong RP hash")
        if not data.is_user_present() or not data.is_user_verified():
            raise Rejected("UP and UV are both required")
        key = CoseKey.parse(cbor.decode(binding.cose_key))
        key.verify(evidence.auth_data + request.digest, evidence.signature)
    except Rejected:
        raise
    except Exception as e:
        raise Rejected("Malformed or invalid assertion") from e


class Gate:
    def __init__(
        self, path: str | Path, binding: Binding, monotonic: Callable[[], float] = time.monotonic
    ):
        self.binding, self.clock = binding, monotonic
        self.owner = secrets.token_hex(16)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS requests (
            nonce TEXT PRIMARY KEY, owner TEXT NOT NULL, encoded BLOB NOT NULL,
            expires REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0)"""
        )

    def close(self):
        self.db.close()

    def create(self, threshold_mdegc: int, ttl: float = 60) -> Request:
        if type(threshold_mdegc) is not int or not -40000 <= threshold_mdegc <= 125000:
            raise ValueError("Threshold must be an integer in [-40000,125000]")
        if not 0 < ttl <= 3600:
            raise ValueError("TTL must be in (0,3600]")
        p = {
            "protocol": "fido-node-maintenance-v1",
            "node_id": self.binding.node_id,
            "operation": "set_alarm_threshold",
            "parameters": {"threshold_mdegc": threshold_mdegc},
            "nonce": secrets.token_hex(32),
        }
        request = Request(canonical(p))
        with self.lock:
            self.db.execute(
                "INSERT INTO requests(nonce,owner,encoded,expires) VALUES(?,?,?,?)",
                (request.nonce, self.owner, request.encoded, self.clock() + ttl),
            )
        return request

    def consume(self, request: Request, evidence: Evidence) -> dict:
        verify(self.binding, request, evidence)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute(
                    "SELECT owner,encoded,expires,consumed FROM requests WHERE nonce=?",
                    (request.nonce,),
                ).fetchone()
                if row is None or row[0] != self.owner:
                    raise Rejected("No pending request in this verifier session")
                if row[1] != request.encoded:
                    raise Rejected("Parameters differ from pending request")
                if row[3] or self.clock() >= row[2]:
                    raise Rejected("Approval consumed or expired")
                self.db.execute("UPDATE requests SET consumed=1 WHERE nonce=?", (request.nonce,))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return request.payload

    def execute(self, request: Request, evidence: Evidence, node) -> dict:
        p = self.consume(request, evidence)
        return node.apply(p["nonce"], p["parameters"]["threshold_mdegc"])
