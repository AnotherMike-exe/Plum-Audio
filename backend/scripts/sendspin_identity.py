#!/usr/bin/env python3
"""
Plum-Audio — the unit's CRYPTOGRAPHIC identity, and who it trusts.

Distinct from `unit_identity`, which resolves the unit's *display name*. This module owns the X25519
keypairs that aiosendspin 9.x made load-bearing, and the pairing stores that go with them.

**Why this exists at all.** Until 9.x a Sendspin id was a string we chose: `PLUM_UNIT_ID` became the
server_id and `PLUM_PLAYER_ID` the client_id, and the mesh could join Sendspin ids against its own
unit table for free. 9.x derives both from a keypair — `SendspinServer(identity=...)` sets
`self._id = identity.peer_id`, the base64url public key — and the client hello no longer carries a
client_id at all. So the ids are now:

  - **not ours to choose**, so the mesh table join has to be made explicit (see `mesh/follow.py`);
  - **not derivable**, so they must be PERSISTED — a regenerated keypair is a brand-new device that
    every peer has forgotten, which is why these files matter more than they look.

**Trust model: trust-on-deploy** (decided 2026-08-12, see docs/AIOSENDSPIN-BUMP-SCOPE.md). We do not
run PIN/PSK pairing. Instead each of our clients sets `unpaired_access_enabled` and each of our
servers calls `trust_unpaired(peer_id)` for the peers it should serve. **Both halves are required and
neither is sufficient** — measured, see `tests/Integration/t0_sendspin_protocol.py`:

    client unpaired_access | server trust_unpaired | negotiated | ACTIVATED
    -----------------------+-----------------------+------------+-----------
             no            |          no           | player@v1  |    -
             yes           |          no           | player@v1  |    -
             no            |          yes          | player@v1  |    -
             yes           |          yes          | player@v1  | player@v1

Miss either and the endpoint connects, negotiates its role, joins the group at the right volume and
renders **nothing**, with no error at either end. The role is always negotiated, so nothing about the
connection distinguishes a working endpoint from a dead one except `active_role_ids`.

**Where the files live.** `/config/identity/`, not `/data`: these are closer to a device certificate
than to runtime state, they must survive a `/data` wipe, and the private keys want the same handling
as the rest of `/config`. `PLUM_IDENTITY_DIR` overrides for tests and for a unit run outside the
container. Keys are written `0o600`; aiosendspin's own file pairing stores do the same for theirs,
ignoring umask by construction, so `UMASK=002` does not apply to either.
"""

from __future__ import annotations

import logging
import os

from aiosendspin.noise import FileClientPairingStore, FileServerPairingStore, Identity
from aiosendspin.noise.trust_store import ClientPairingConfig

logger = logging.getLogger("plum.sendspin_identity")

DEFAULT_IDENTITY_DIR = "/config/identity"

# The two roles a unit runs. Named rather than free-form because the SERVER creates both (see
# load_or_create) and a typo would silently mint a second identity instead of reusing one.
SERVER_ROLE = "server"
PLAYER_ROLE = "player"


def identity_dir() -> str:
    return os.environ.get("PLUM_IDENTITY_DIR", DEFAULT_IDENTITY_DIR)


def key_path(role: str) -> str:
    return os.path.join(identity_dir(), f"{role}.key")


def pairing_store_path(role: str) -> str:
    return os.path.join(identity_dir(), f"{role}-pairing.json")


def local_pair_psk_path() -> str:
    return os.path.join(identity_dir(), "local-pair.psk")


def local_pairing_psk() -> bytes:
    """The Pairing PSK this unit's server and its own player share, minted once and persisted.

    A unit's server and its player are two processes on one device, in one container, behind one
    `/config`. They are the same trust domain by construction, so making an operator pair a unit
    with *itself* would be ceremony with no security content — and it would mean a fresh unit's own
    speaker stays silent until a human intervened, on a box that may have no screen attached.
    Music Assistant reached the same conclusion for its built-in web player (their #5591).

    So they pair over the **Pairing PSK** method, which needs no interaction: both ends must simply
    know one secret. This file IS that secret. The player installs it as the PSK it will accept; the
    server presents it in a `PairingAttempt`. The result is a real long-term pairing record with
    trust level `user` — not a sentinel bypass — which is also what earns the server the `management`
    activity on its own player, and that is what makes the provisioning window possible at all.

    Same create-once-or-lose-the-race shape as `load_or_create`, for the same reason: two processes
    minting different secrets would leave a unit unable to pair with itself, silently.
    """
    path = local_pair_psk_path()
    existing = _read_psk(path)
    if existing is not None:
        return existing

    from aiosendspin.noise import generate_psk

    psk = generate_psk()
    os.makedirs(identity_dir(), mode=0o700, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raced = _read_psk(path)
        if raced is None:
            raise
        logger.info("local pairing psk: lost the create race, using the stored secret")
        return raced
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(_b64(psk))
    logger.info("local pairing psk: minted %s", path)
    return psk


def _read_psk(path: str) -> bytes | None:
    """The stored local Pairing PSK, or None. A damaged one is fatal, like a damaged key."""
    from aiosendspin.noise import b64url_decode

    try:
        with open(path, encoding="utf-8") as f:
            stored = f.read().strip()
    except FileNotFoundError:
        return None
    if not stored:
        raise ValueError(f"{path} is empty — refusing to mint a replacement over it")
    return b64url_decode(stored)


def _b64(raw: bytes) -> str:
    from aiosendspin.noise import b64url_encode

    return b64url_encode(raw)


def load_or_create(role: str) -> Identity:
    """This role's persistent identity, minting one on first call.

    Race-safe by construction: the create path uses O_EXCL and falls back to re-reading, because the
    server creates BOTH identities at startup (it has the lower supervisord priority, so it wins the
    race in practice) while the player creates its own if it somehow starts first. Two processes
    minting different keys for the same role would leave the server trusting a peer id the player
    does not have — the silent-dud failure above, arrived at from the other direction.
    """
    path = key_path(role)
    existing = _read_private(path)
    if existing is not None:
        return existing

    identity = Identity.generate()
    os.makedirs(identity_dir(), mode=0o700, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Someone beat us to it between the read and the create. Theirs wins — ours was never used.
        raced = _read_private(path)
        if raced is None:
            raise
        logger.info("identity %s: lost the create race, using the stored key", role)
        return raced
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(identity.private_b64u)
    logger.info("identity %s: minted %s (peer_id %s)", role, path, identity.peer_id)
    return identity


def _read_private(path: str) -> Identity | None:
    """The stored identity, or None. A damaged key is fatal, NOT silently replaced.

    Overwriting an unreadable key would mint a new identity and orphan every trust record naming the
    old one — the unit would come up looking healthy and be unable to play, which is precisely the
    failure this module is built to avoid. Better to crash-loop with a clear reason.
    """
    try:
        with open(path, encoding="utf-8") as f:
            stored = f.read().strip()
    except FileNotFoundError:
        return None
    if not stored:
        raise ValueError(f"{path} is empty — refusing to mint a replacement identity over it")
    return Identity.from_private_bytes(_decode(stored))


def _decode(private_b64u: str) -> bytes:
    from aiosendspin.noise import b64url_decode

    return b64url_decode(private_b64u)


def peer_id_of(role: str) -> str | None:
    """The peer id for a role whose key already exists, without minting one.

    This is how the SERVER learns its own player's client id. Both processes share `/config` inside
    one container, so the server can derive the player's public id from the stored key rather than
    waiting for a handshake it cannot route audio without.
    """
    identity = _read_private(key_path(role))
    return identity.peer_id if identity else None


async def server_pairing_store() -> FileServerPairingStore:
    os.makedirs(identity_dir(), mode=0o700, exist_ok=True)
    return await FileServerPairingStore.open(pairing_store_path(SERVER_ROLE))


async def client_pairing_store(role: str = PLAYER_ROLE, *, unpaired_access: bool | None = None) -> FileClientPairingStore:
    """This client's pairing store, with its policy applied.

    Two things are configured here, and they are independent:

    **The Pairing PSK** this client will accept, so its own unit's server can pair with it without an
    operator — see `local_pairing_psk`. Installed unconditionally: it is what makes a fresh unit's
    own speaker work out of the box.

    **Unpaired access**, which is the sentinel-PSK escape hatch. `_playback_capable` requires BOTH
    this flag on the client AND `trust_unpaired(peer_id)` on the server, so with it off an
    unpaired encrypted client is admitted and activated for nothing. It defaults OFF now that real
    pairing exists; `unpaired_access=None` leaves whatever is stored alone.

    Note this does NOT touch cleartext clients — ESP32 speakers, Music Assistant, our own web GUI.
    They never reach the trust gate at all.
    """
    from aiosendspin.noise import PairingPsk, psk_id_for

    os.makedirs(identity_dir(), mode=0o700, exist_ok=True)
    store = await FileClientPairingStore.open(pairing_store_path(role))

    psk = local_pairing_psk()
    stored = await store.pairing_psk()
    if stored is None or stored.psk != psk:
        await store.set_pairing_psk(PairingPsk(psk_id=psk_id_for(psk), psk=psk))
        logger.info("pairing store %s: installed the local Pairing PSK", role)

    config = await store.get_pairing_config()
    if unpaired_access is not None and config.unpaired_access_enabled != unpaired_access:
        await store.store_pairing_config(
            ClientPairingConfig(
                unpaired_access_enabled=unpaired_access,
                record_mode_psk_id=config.record_mode_psk_id,
                pairing_psk_enabled=config.pairing_psk_enabled,
                dynamic_pin_enabled=config.dynamic_pin_enabled,
                static_pin_enabled=config.static_pin_enabled,
                dynamic_pin_min_length=config.dynamic_pin_min_length,
            )
        )
        logger.info("pairing store %s: unpaired access -> %s", role, "on" if unpaired_access else "off")
    return store


def allow_unencrypted() -> bool:
    """Whether to accept cleartext `client/hello` connections. Defaults TRUE, deliberately.

    Upstream defaults this False and calls the cleartext path "non-spec transition mode". For us it
    is not transitional: `sendspin-cpp` — what ESPHome's Sendspin component, the HA Voice PE and the
    Esparagus/Satellite1 boards run — has no Noise, PSK or PIN support in any release as of v0.7.2.
    Turning this off drops every third-party speaker on the LAN, plus Music Assistant.

    It is an env var rather than a constant so the day sendspin-cpp ships encryption we can test a
    unit with it off without a rebuild — and so the choice is visible in the compose file rather than
    buried in a constructor call.
    """
    return os.environ.get("PLUM_ALLOW_UNENCRYPTED", "1").strip().lower() not in ("0", "false", "no")
