#!/usr/bin/env bash
# Run a rig's integration suite. See README.md.
#   ./run.sh mesh    <unit-a> <unit-b>   — tier 2 (on unit-a) + tier 3 roam
#   ./run.sh interop <unit>              — tier 2 + tier 4 (Music Assistant on-segment)
set -uo pipefail
cd "$(dirname "$0")"

MODE="${1:?usage: run.sh <mesh|interop> <host...>}"; shift
rc=0
run() { echo; echo "### $*"; bash "$@" || rc=1; }

case "$MODE" in
    mesh)
        A="${1:?need unit-a}"; B="${2:?need unit-b}"
        run t2_source_lifecycle.sh "$A"
        run t2_endpoint_crud.sh "$A" spotify
        run t3_mesh_roam.sh "$A" "$B"
        run t3_autofollow.sh "$A" "$B"
        ;;
    interop)
        U="${1:?need unit host}"
        run t2_source_lifecycle.sh "$U"
        run t2_endpoint_crud.sh "$U" spotify
        run t4_interop_ma.sh "$U"
        run t4_adopt_release.sh "$U"
        ;;
    *) echo "unknown mode '$MODE' (want: mesh | interop)"; exit 2 ;;
esac

echo; [[ $rc -eq 0 ]] && echo "### ALL SUITES PASSED" || echo "### SOME SUITES FAILED"
exit $rc
