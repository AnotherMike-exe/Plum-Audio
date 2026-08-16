#!/usr/bin/env python3
"""
About/version API — what the running unit's About page shows.

The app's own version can only ever be what was baked in at build time (docker/build.sh derives it
from `git describe` and passes it as a Docker build-arg — see backend/Dockerfile), so that piece is
static per-image. Everything else here is queried LIVE against the running container instead of
mirroring a version string that would drift the moment the base image or a pinned dependency moves —
aiosendspin via package metadata, shairport-sync/bluez-alsa via the binaries/dpkg actually installed.
go-librespot has no package metadata to query, so it tries `--version` first and falls back to the
version baked in at image build time (`PLUM_GO_LIBRESPOT_VERSION`, from the same ARG that picked which
release to download in the Dockerfile).
"""

import importlib.metadata as md
import logging
import os
import re
import shutil
import subprocess

from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)


def _run_version(argv: list[str]) -> str | None:
    exe = shutil.which(argv[0])
    if not exe:
        return None
    try:
        result = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("version probe for %s failed: %s", argv[0], exc)
        return None
    text = (result.stdout or result.stderr or "").strip()
    return text or None


def _aiosendspin_version() -> str | None:
    try:
        return md.version("aiosendspin")
    except md.PackageNotFoundError:
        return None


def _shairport_sync_version() -> str | None:
    # `shairport-sync -V` answers e.g. "4.3.7-OpenSSL-Avahi-ALSA-soxr-metadata-sysconfdir:/etc-mpris"
    # — lead with the bare version, keep the rest as detail rather than discarding it.
    raw = _run_version(["shairport-sync", "-V"])
    if not raw:
        return None
    match = re.match(r"^(\d+\.\d+(?:\.\d+)?)", raw)
    return match.group(1) if match else raw


def _go_librespot_version() -> str | None:
    return _run_version(["go-librespot", "--version"]) or os.environ.get("PLUM_GO_LIBRESPOT_VERSION")


def _bluez_alsa_version() -> str | None:
    exe = shutil.which("dpkg-query")
    if not exe:
        return None
    try:
        result = subprocess.run(
            [exe, "-W", "-f=${Version}", "bluez-alsa-utils"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("dpkg-query for bluez-alsa-utils failed: %s", exc)
        return None
    return result.stdout.strip() or None


def create_about_blueprint() -> Blueprint:
    bp = Blueprint("about", __name__)

    @bp.route("/api/about/versions", methods=["GET"])
    def get_versions():
        return jsonify(
            {
                "app": {
                    "version": os.environ.get("PLUM_APP_VERSION", "0.0.0-dev"),
                    "buildType": os.environ.get("PLUM_BUILD_TYPE", "dev"),
                    "gitDescribe": os.environ.get("PLUM_GIT_DESCRIBE"),
                },
                "sendspin": {"aiosendspin": _aiosendspin_version()},
                "airplay": {"shairportSync": _shairport_sync_version()},
                "spotify": {"goLibrespot": _go_librespot_version()},
                "bluetooth": {"bluezAlsa": _bluez_alsa_version()},
            }
        )

    return bp
