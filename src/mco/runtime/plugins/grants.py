"""Verify runtime-bound, single-use grants from the configured native confirmation issuer."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import Field, ValidationError

from .contract import Action, Contract, Name, Scalar
from .trust import Candidate, canonical, tomllib


MAX_GRANT_SECONDS = 60


class Grant(Contract):
    nonce: str = Field(pattern=r"^[a-f0-9]{32}$")
    owner: Name
    plugin: Name
    action: Name
    parameters: dict[Name, Scalar] = Field(max_length=32)
    trust: str = Field(pattern=r"^[a-f0-9]{64}$")
    audience: str = Field(pattern=r"^[a-f0-9]{32}$")
    issued_at: float
    expires_at: float
    verification: Literal["confirmed", "platform-user-verified"]


class SignedGrant(Contract):
    claims: Grant
    signature: str = Field(pattern=r"^[a-f0-9]{128}$")


class GrantError(ValueError):
    pass


def grant_message(claims: Grant) -> bytes:
    return b"bitcadence.owner-grant/1\n" + canonical(claims.model_dump())


def read_owner(path: Path) -> tuple[str | None, Ed25519PublicKey | None]:
    try:
        with path.open("rb") as stream:
            config = tomllib.load(stream)
    except FileNotFoundError:
        return None, None
    instance = config["instance"]
    # The same schema controls the owner identity in incoming grants.
    if not isinstance(instance, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", instance):
        raise ValueError("invalid plugin owner identity")
    return instance, Ed25519PublicKey.from_public_bytes(bytes.fromhex(config["public_key"]))


def parameter_argv(action: Action, parameters: dict) -> list[str]:
    if parameters.keys() != {item.name for item in action.parameters}:
        raise GrantError("parameters do not match the action")
    argv = []
    types = {"string": str, "integer": int, "boolean": bool}
    for item in action.parameters:
        value = parameters[item.name]
        if type(value) is not types[item.type] or (item.choices and value not in item.choices):
            raise GrantError("invalid action parameter")
        if isinstance(value, str):
            if len(value) > 4096 or any(char in value for char in "\0\r\n"):
                raise GrantError("invalid action parameter")
            argv.append(value)
        elif isinstance(value, bool):
            argv.append("true" if value else "false")
        else:
            if not -(2**63) <= value < 2**63:
                raise GrantError("invalid action parameter")
            argv.append(str(value))
    return argv


def verify_grant(data, *, key, owner, audience, candidate: Candidate, action_id, parameters, now) -> Grant:
    if key is None or owner is None:
        raise GrantError("owner grant issuer is not configured")
    try:
        envelope = SignedGrant.model_validate(data)
        claims = envelope.claims
        key.verify(bytes.fromhex(envelope.signature), grant_message(claims))
    except (InvalidSignature, ValidationError, ValueError):
        raise GrantError("invalid owner grant") from None
    if (
        claims.owner != owner
        or claims.audience != audience
        or claims.plugin != candidate.manifest.id
        or claims.action != action_id
        or claims.trust != candidate.fingerprint
        or canonical(claims.parameters) != canonical(parameters)
        or not claims.issued_at <= now < claims.expires_at <= claims.issued_at + MAX_GRANT_SECONDS
    ):
        raise GrantError("owner grant does not match this invocation or expired")
    if candidate.manifest.actions[action_id].dangerous and claims.verification != "platform-user-verified":
        raise GrantError("platform user verification is required")
    return claims
