#!/usr/bin/env bash
# Build the Plum-Audio unit image and save it as a loadable tarball.
#
# There is no registry in the loop: the R&D units are three Pis on two VLANs, and `docker save` +
# scp + `docker load` beats standing up (and authenticating to) a registry for a rig this size.
# deploy.sh consumes the tarball this writes.
#
#   ./build.sh                 # build linux/arm64 (the Pis) and save dist/plum-audio-<tag>.tar.gz
#   PLUM_PLATFORM=linux/amd64 ./build.sh
#   PLUM_TAG=phase3 ./build.sh
#
# On Apple Silicon the arm64 build is native — no emulation, no qemu install.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

PLATFORM="${PLUM_PLATFORM:-linux/arm64}"
# Default tag is the commit the image was built from, so a unit's `docker images` answers "which
# code is this?" without guessing. Dirty trees are marked — the rig runs uncommitted code often.
if [[ -z "${PLUM_TAG:-}" ]]; then
    PLUM_TAG="$(git rev-parse --short HEAD 2>/dev/null || echo notgit)"
    [[ -n "$(git status --porcelain 2>/dev/null)" ]] && PLUM_TAG="${PLUM_TAG}-dirty"
fi

IMAGE="plum-audio:${PLUM_TAG}"
OUT_DIR="${ROOT}/dist"
ARCH_SUFFIX="${PLATFORM##*/}"
TARBALL="${OUT_DIR}/plum-audio-${PLUM_TAG}-${ARCH_SUFFIX}.tar.gz"

# The app version shown on the About page and the main-page footer. Nearest tag, "-dev" appended
# unless the tree is exactly at that tag with nothing uncommitted — so "0.4.2" only ever means the
# 0.4.2 release, and any other build reads "0.4.2-dev" rather than silently claiming to be the release.
GIT_DESCRIBE="$(git describe --tags --always --dirty 2>/dev/null || echo unknown)"
LAST_TAG="$(git describe --tags --abbrev=0 2>/dev/null || echo v0.0.0)"
if git describe --tags --exact-match >/dev/null 2>&1 && [[ -z "$(git status --porcelain 2>/dev/null)" ]]; then
    PLUM_APP_VERSION="${LAST_TAG#v}"
    PLUM_BUILD_TYPE="release"
else
    PLUM_APP_VERSION="${LAST_TAG#v}-dev"
    PLUM_BUILD_TYPE="dev"
fi

echo "==> building ${IMAGE} for ${PLATFORM} (app version ${PLUM_APP_VERSION}, ${PLUM_BUILD_TYPE})"
docker build \
    --platform "$PLATFORM" \
    -f backend/Dockerfile \
    --build-arg "PLUM_APP_VERSION=${PLUM_APP_VERSION}" \
    --build-arg "PLUM_BUILD_TYPE=${PLUM_BUILD_TYPE}" \
    --build-arg "PLUM_GIT_DESCRIBE=${GIT_DESCRIBE}" \
    -t "$IMAGE" \
    -t "plum-audio:latest" \
    "$ROOT"

mkdir -p "$OUT_DIR"
echo "==> saving ${TARBALL}"
# Both tags ride in one tarball (same image id): the unit gets the traceable tag AND the :latest the
# compose file defaults to. gzip -1 — this is a LAN copy, so trading ~15% size for a much faster
# compress is the right side of the deal.
docker save "$IMAGE" "plum-audio:latest" | gzip -1 > "$TARBALL"

echo
echo "    image:   ${IMAGE}  ($(docker image inspect "$IMAGE" --format '{{.Size}}' | awk '{printf "%.0f MB", $1/1024/1024}'))"
echo "    tarball: ${TARBALL}  ($(du -h "$TARBALL" | cut -f1))"
echo
echo "next: docker/deploy.sh all --tarball ${TARBALL}"
