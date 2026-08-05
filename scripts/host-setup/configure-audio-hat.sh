#!/usr/bin/env bash
#
# Plum-Audio — provision a Raspberry Pi audio HAT on the HOST.
#
# WHY THIS EXISTS
#   Raspberry Pi OS does not auto-detect audio HATs. Until the right device-tree overlay is in
#   config.txt the card simply is not there — `aplay -l` does not list it, PortAudio cannot enumerate
#   it, and the output picker in the GUI has nothing to offer. Plum-Snapcast documented this as a
#   manual edit in its README and shipped no code for it; this is that missing piece.
#
#   It is HOST provisioning, not a container concern, for the same reason the bluez patches and the
#   Avahi/D-Bus setup are: the kernel owns the device tree, and config.txt is read by the bootloader
#   long before Docker exists.
#
# THE PART THAT IS NOT ABOUT config.txt
#   A HAT's own hardware mixer is restored at every boot by alsa-restore, from
#   /var/lib/alsa/asound.state — and it does NOT default to unity. A HiFiBerry Amp100 measured on
#   2026-08-04 came up with `Digital` at 163/207, which is -22 dB. Nothing in Plum-Audio touches that
#   control: the player applies volume as software gain in the PortAudio callback (see
#   backend/scripts/audio_devices.py, and the three-volume model in CLAUDE.md). So a HAT unit runs
#   22 dB quiet with every slider in the mesh reading correct — the exact class of failure this
#   project keeps finding. `--unity` pins the control and persists it with alsactl store.
#
#   Read that as: on a power amplifier this makes the unit LOUDER, potentially a lot louder, the
#   moment it is applied. Do it before you set listening levels, not during a party.
#
# WHY PLUM-SNAPCAST NEVER NEEDED THIS
#   It did not have the problem, because it solved volume differently: snapclient was launched with
#   `--mixer hardware:${AUDIO_MIXER_NAME}` (backend/config/supervisord/snapclient.ini), so snapclient
#   OWNED the HAT's `Digital` control and its own level overwrote whatever alsa-restore had put
#   there. `audio_devices.py` detected the control and `get-settings.py` passed it through. There is
#   no amixer or alsactl call anywhere in that repo — it never had to pin anything.
#
#   Plum-Audio applies volume as software gain in the PortAudio callback instead, so nothing touches
#   the hardware control and the attenuation just sits there. Pinning to unity is the minimal fix
#   that fits the three-volume model. The honest trade-off versus Snapcast: at low listening levels
#   16-bit software attenuation loses resolution a hardware mixer would not (~2 bits at 25%). If that
#   ever matters audibly, the alternative is to teach the renderer to drive the ALSA mixer — a
#   deliberate change to the volume model, not a tweak.
#
#   Snapcast also half-anticipated the card-numbering problem: get-settings.py translated `hw:X,Y`
#   into `default:CARD=<name>` by reading /proc/asound/cards at launch. But it still PERSISTED the
#   number, so a card that renumbered between saving and booting would translate the stale number to
#   the wrong card's name. Plum-Audio persists the name itself — see backend/scripts/audio_devices.py.
#
# WHAT IT DOES
#   Writes an idempotent, clearly-marked block into /boot/firmware/config.txt (or /boot/config.txt on
#   older images). Everything outside the markers is left exactly as found; re-running rewrites only
#   the block. A conflicting `dtparam=audio=on` outside the block is commented out, with the original
#   line preserved beside it, because the onboard codec otherwise competes for card 0.
#
# USAGE (on the unit, as root)
#   sudo ./configure-audio-hat.sh --list                 # supported overlays
#   sudo ./configure-audio-hat.sh --detect               # what is fitted / configured right now
#   sudo ./configure-audio-hat.sh --overlay hifiberry-amp100
#   sudo ./configure-audio-hat.sh --overlay hifiberry-amp100 --keep-onboard
#   sudo ./configure-audio-hat.sh --unity                # mixer only, no reboot needed
#   sudo ./configure-audio-hat.sh --revert               # remove our block, restore onboard audio
#
# WHY THERE IS NO AUTO-DETECT
#   HATs with an ID EEPROM expose /proc/device-tree/hat/{product,vendor} and could in principle pick
#   their own overlay. The Amp100 on this rig exposes NOTHING there — the directory does not exist —
#   so EEPROM detection would silently do nothing on the one board we have to support. --detect
#   reports the EEPROM when present and is honest about it when absent; choosing is the operator's.
#
# AFTER A REBOOT
#   Card NUMBERS are not stable and the unit does not depend on them: Plum-Audio stores the ALSA card
#   NAME. Verified on this rig — a HiFiBerry moved from card 2 to card 1 across a single reboot with
#   nothing else changed. Do not "fix" anything that references hw:N,M.

set -euo pipefail

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m warn\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31mfail\033[0m %s\n' "$*" >&2; exit 1; }

BEGIN_MARK="# >>> plum-audio hat (managed — edit via scripts/host-setup/configure-audio-hat.sh) >>>"
END_MARK="# <<< plum-audio hat <<<"

# Curated because the alternative is a wrong guess that boots to silence. Extend freely: the value on
# the left is what goes into config.txt, and the kernel is the authority on whether it loads.
SUPPORTED_OVERLAYS=(
    "hifiberry-dac:HiFiBerry DAC / MiniAmp (PCM5102A, no hardware mixer)"
    "hifiberry-dacplus:HiFiBerry DAC+ / DAC+ Pro / Amp2 (PCM5122)"
    "hifiberry-dacplushd:HiFiBerry DAC+ HD (PCM1796)"
    "hifiberry-amp100:HiFiBerry Amp100 (reports as DAC+ Pro — same pcm512x driver)"
    "hifiberry-amp3:HiFiBerry Amp3"
    "hifiberry-digi:HiFiBerry Digi / Digi+ (S/PDIF)"
    "iqaudio-dac:IQaudIO Pi-DAC / Pi-DACZero"
    "iqaudio-dacplus:IQaudIO Pi-DAC+ / Pi-DigiAMP+"
    "allo-boss-dac-pcm512x-audio:Allo Boss DAC"
    "allo-piano-dac-pcm512x-audio:Allo Piano DAC"
    "justboom-dac:JustBoom DAC / Amp"
    "justboom-digi:JustBoom Digi"
    "audioinjector-wm8731-audio:Audio Injector Stereo"
    "rpi-dac:Generic PCM5102A / rpi-dac"
)

# Priority order for "the control that is really the output level". First match on the card wins.
MIXER_CANDIDATES=(Digital PCM Master Playback Speaker Analogue)

CONFIG=""
OVERLAY=""
KEEP_ONBOARD=0
DO_UNITY=0
DO_REVERT=0
DO_LIST=0
DO_DETECT=0
MIXER_PERCENT=100

for ((i = 1; i <= $#; i++)); do
    case "${!i}" in
        # i=$((i+1)) rather than ((i++)): the latter returns non-zero when the old value was 0, and
        # under `set -e` that aborts the script rather than parsing the next argument.
        --overlay)       i=$((i + 1)); OVERLAY="${!i:-}" ;;
        --overlay=*)     OVERLAY="${!i#*=}" ;;
        --mixer-percent) i=$((i + 1)); MIXER_PERCENT="${!i:-100}" ;;
        --mixer-percent=*) MIXER_PERCENT="${!i#*=}" ;;
        --keep-onboard)  KEEP_ONBOARD=1 ;;
        --unity)         DO_UNITY=1 ;;
        --revert)        DO_REVERT=1 ;;
        --list)          DO_LIST=1 ;;
        --detect)        DO_DETECT=1 ;;
        -h|--help)       awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
        *)               die "unknown argument: ${!i}" ;;
    esac
done

list_overlays() {
    printf '%-32s %s\n' "OVERLAY" "BOARD"
    for entry in "${SUPPORTED_OVERLAYS[@]}"; do
        printf '%-32s %s\n' "${entry%%:*}" "${entry#*:}"
    done
    echo
    echo "Not listed? Any overlay name your kernel ships works — see /boot/firmware/overlays/."
}

[[ "$DO_LIST" == 1 ]] && { list_overlays; exit 0; }

# PLUM_CONFIG_TXT points this at a fixture instead of the real boot partition, which is what lets
# tests/Unit/test_configure_audio_hat.py exercise the block editing off-Pi. Every bug this script has
# had so far was in that text manipulation, not in the hardware handling.
find_config() {
    if [[ -n "${PLUM_CONFIG_TXT:-}" ]]; then
        [[ -f "$PLUM_CONFIG_TXT" ]] || die "PLUM_CONFIG_TXT=$PLUM_CONFIG_TXT does not exist"
        echo "$PLUM_CONFIG_TXT"
        return
    fi
    for candidate in /boot/firmware/config.txt /boot/config.txt; do
        [[ -f "$candidate" ]] && { echo "$candidate"; return; }
    done
    die "no config.txt found at /boot/firmware or /boot — is this a Raspberry Pi?"
}

# The card whose ALSA name looks like an add-on board rather than the SoC's own outputs.
#
# Plain field splitting on purpose. The obvious `match($0, /re/, m)` is a GAWK extension and Debian
# ships MAWK, where it is a syntax error — which this function swallowed, so --unity reported "no HAT
# card found" on a unit with a HAT plainly listed in aplay -l. Caught on .7.204 on 2026-08-04.
#   card 1: sndrpihifiberry [snd_rpi_hifiberry_dacplus], device 0: ...
#   $1     $2  $3
detect_hat_card() {
    aplay -l 2>/dev/null | awk '
        /^card [0-9]+:/ {
            if ($0 ~ /vc4hdmi|bcm2835|Headphones/) next
            num = $2; sub(/:$/, "", num)
            print num " " $3
            exit
        }'
}

detect() {
    say "boot config: $(find_config)"
    echo "  managed block: $(grep -qF "$BEGIN_MARK" "$(find_config)" && echo present || echo absent)"
    echo "  onboard audio: $(grep -qE '^\s*dtparam=audio=on' "$(find_config)" && echo on || echo off)"
    echo "  overlays currently set:"
    grep -E '^\s*dtoverlay=' "$(find_config)" | sed 's/^/    /' || echo "    (none)"

    echo
    say "HAT ID EEPROM"
    if [[ -d /proc/device-tree/hat ]]; then
        for f in /proc/device-tree/hat/*; do
            [[ -f "$f" ]] && printf '  %s = %s\n' "$(basename "$f")" "$(tr -d '\0' < "$f")"
        done
    else
        echo "  none — this board does not expose one (normal; the Amp100 does not either)."
        echo "  Pick the overlay yourself with --list."
    fi

    echo
    say "sound cards now"
    aplay -l 2>/dev/null | grep '^card' | sed 's/^/  /' || echo "  none"

    local found
    found="$(detect_hat_card)"
    if [[ -n "$found" ]]; then
        local card="${found%% *}"
        echo
        say "looks like the HAT is card ${card} (${found#* })"
        echo "  mixer controls:"
        amixer -c "$card" scontrols 2>/dev/null | sed 's/^/    /' || echo "    (none)"
        for control in "${MIXER_CANDIDATES[@]}"; do
            if amixer -c "$card" sget "$control" >/dev/null 2>&1; then
                echo "  current '$control':"
                amixer -c "$card" sget "$control" 2>/dev/null | grep -E 'Front (Left|Right):' | sed 's/^/    /'
                break
            fi
        done
    fi
}

# Pin the HAT's own output control to unity and persist it, so alsa-restore stops reinstating an
# attenuation nothing in Plum-Audio can see or correct.
set_unity() {
    local found card control
    found="$(detect_hat_card)"
    [[ -n "$found" ]] || die "no HAT card found in aplay -l — configure the overlay and reboot first"
    card="${found%% *}"

    control=""
    for candidate in "${MIXER_CANDIDATES[@]}"; do
        if amixer -c "$card" sget "$candidate" >/dev/null 2>&1; then control="$candidate"; break; fi
    done
    if [[ -z "$control" ]]; then
        warn "card ${card} exposes no recognised volume control — nothing to pin (a DAC with no"
        warn "hardware mixer, e.g. plain PCM5102A, is already effectively at unity)"
        return 0
    fi

    say "pinning '${control}' on card ${card} to ${MIXER_PERCENT}%"
    echo "  before: $(amixer -c "$card" sget "$control" | grep -m1 -oE '\[[0-9]+%\] \[[-0-9.]+dB\]' || echo '?')"
    amixer -c "$card" sset "$control" "${MIXER_PERCENT}%" unmute >/dev/null
    echo "  after:  $(amixer -c "$card" sget "$control" | grep -m1 -oE '\[[0-9]+%\] \[[-0-9.]+dB\]' || echo '?')"

    # Without this, alsa-restore puts the old level back at the next boot.
    alsactl store 2>/dev/null || warn "alsactl store failed — the level will not survive a reboot"
    say "stored to /var/lib/alsa/asound.state"
}

# Remove a previously written block, matching the markers as LITERAL text. Deliberately awk and not
# a sed range: the markers contain /, >, ( and ), and escaping them into a sed address is the kind of
# quoting that works until someone edits the marker text and then silently deletes the wrong lines
# out of config.txt.
strip_block() {
    local config="$1" tmp="${1}.plumtmp"
    grep -qF "$BEGIN_MARK" "$config" || return 0
    # The second pass drops trailing blank lines. Each apply prepends one as a separator, so without
    # this an apply/revert cycle leaves a blank line behind every time and config.txt slowly grows.
    awk -v b="$BEGIN_MARK" -v e="$END_MARK" '
        $0 == b { skip = 1 }
        !skip   { print }
        $0 == e { skip = 0 }
    ' "$config" | awk '
        { lines[NR] = $0 }
        END {
            last = NR
            while (last > 0 && lines[last] ~ /^[[:space:]]*$/) last--
            for (i = 1; i <= last; i++) print lines[i]
        }
    ' > "$tmp"
    # cat back rather than mv: config.txt lives on the vfat boot partition and the bootloader wants
    # the file where it is, with its own permissions, not a fresh inode.
    cat "$tmp" > "$config"
    rm -f "$tmp"
}

# `sed -i` takes no argument on GNU and a mandatory one on BSD, and `\?` is a GNU-only extension.
# The script only ever RUNS on a Pi, but keeping to POSIX sed is what lets the config.txt editing be
# tested on a dev machine (tests/Unit/test_configure_audio_hat.py) — where every bug in it has been.
edit_in_place() {  # edit_in_place <file> <sed-expression>...
    local file="$1" tmp="${1}.plumtmp"
    shift
    local args=()
    for expr in "$@"; do args+=(-e "$expr"); done
    sed "${args[@]}" "$file" > "$tmp"
    cat "$tmp" > "$file"
    rm -f "$tmp"
}

print_block() {
    awk -v b="$BEGIN_MARK" -v e="$END_MARK" '
        $0 == b { inb = 1 }
        inb     { print "    " $0 }
        $0 == e { inb = 0 }
    ' "$1"
}

# Comment out audio dtoverlay lines we did not write.
#
# This is the ADOPTION case, and it is the common one: Plum-Snapcast's README told people to add
# `dtoverlay=hifiberry-amp100` to config.txt by hand, so a unit moving to Plum-Audio already has one.
# Appending the managed block without this leaves the overlay declared twice. Only overlays from the
# curated list (plus the requested one) are touched — vc4-kms-v3d, dwc2 and friends are none of our
# business. Commented, not deleted, and with the original text intact, so --revert is exact.
disable_conflicting_overlays() {
    local config="$1" target="$2" name
    local names=("$target")
    for entry in "${SUPPORTED_OVERLAYS[@]}"; do names+=("${entry%%:*}"); done
    for name in "${names[@]}"; do
        # Two expressions rather than one with `\?`: bare `dtoverlay=x`, and `dtoverlay=x,args`.
        edit_in_place "$config" \
            "s|^\([[:space:]]*dtoverlay=${name}\)[[:space:]]*$|#plum-audio-disabled: \1|" \
            "s|^\([[:space:]]*dtoverlay=${name}[,[:space:]].*\)$|#plum-audio-disabled: \1|"
    done
}

main() {
    CONFIG="$(find_config)"
    # Root is needed to write the boot partition or drive amixer/alsactl — not to edit a fixture.
    if [[ "$CONFIG" == /boot/* || "$DO_UNITY" == 1 ]]; then
        [[ "$EUID" -eq 0 ]] || die "run as root (sudo $0 ...)"
    fi

    if [[ "$DO_DETECT" == 1 ]]; then detect; exit 0; fi
    if [[ "$DO_UNITY" == 1 && -z "$OVERLAY" && "$DO_REVERT" == 0 ]]; then set_unity; exit 0; fi

    if [[ "$DO_REVERT" == 1 ]]; then
        cp -a "$CONFIG" "${CONFIG}.plum.bak"
        strip_block "$CONFIG"
        # Put back whatever onboard line we commented out.
        edit_in_place "$CONFIG" 's|^#plum-audio-disabled: *||'
        say "removed the managed block (backup: ${CONFIG}.plum.bak)"
        say "reboot to apply"
        exit 0
    fi

    [[ -n "$OVERLAY" ]] || die "nothing to do — pass --overlay <name>, --unity, --detect or --list"

    if [[ -f "/boot/firmware/overlays/${OVERLAY}.dtbo" || -f "/boot/overlays/${OVERLAY}.dtbo" ]]; then
        say "overlay ${OVERLAY} found in the kernel's overlay directory"
    else
        warn "no ${OVERLAY}.dtbo on this system — the name may be wrong, or the kernel may be older"
        warn "than the board. Continuing: an overlay that fails to load is inert, not harmful."
    fi

    cp -a "$CONFIG" "${CONFIG}.plum.bak"
    strip_block "$CONFIG"

    disable_conflicting_overlays "$CONFIG" "$OVERLAY"

    if [[ "$KEEP_ONBOARD" == 0 ]]; then
        # Comment rather than delete, and keep the original text, so --revert is exact.
        edit_in_place "$CONFIG" 's|^\([[:space:]]*dtparam=audio=on[[:space:]]*\)$|#plum-audio-disabled: \1|'
    fi

    # POSITION MATTERS — do not "simplify" this to an append.
    #
    # The block goes BEFORE the first existing dtoverlay line, which is where Raspberry Pi's own
    # config.txt keeps audio settings. Appending it after `dtoverlay=vc4-kms-v3d` instead costs an
    # HDMI audio output: measured on .7.204, overlay before vc4-kms-v3d enumerates vc4hdmi0,
    # vc4hdmi1 and the HAT (3 boots), while overlay after it enumerates only vc4hdmi1 and the HAT
    # (2 boots) — vc4hdmi0 never appears. The HAT itself works either way, which is exactly why this
    # would have shipped unnoticed on a unit nobody plugs a display into.
    local block="${CONFIG}.plumblock"
    {
        echo "$BEGIN_MARK"
        [[ "$KEEP_ONBOARD" == 0 ]] && echo "dtparam=audio=off"
        echo "dtoverlay=${OVERLAY}"
        echo "$END_MARK"
    } > "$block"

    local tmp="${CONFIG}.plumtmp"
    awk -v blockfile="$block" '
        !placed && /^[[:space:]]*dtoverlay=/ {
            while ((getline line < blockfile) > 0) print line
            close(blockfile)
            placed = 1
        }
        { print }
        END {
            if (!placed) {          # no dtoverlay lines at all — end of file is the only choice
                print ""
                while ((getline line < blockfile) > 0) print line
            }
        }
    ' "$CONFIG" > "$tmp"
    cat "$tmp" > "$CONFIG"
    rm -f "$tmp" "$block"

    say "wrote the managed block to ${CONFIG} (backup: ${CONFIG}.plum.bak)"
    print_block "$CONFIG"

    echo
    say "REBOOT to load the overlay, then:"
    echo "    aplay -l                                   # the HAT should be listed"
    echo "    sudo $0 --unity                            # pin its mixer (see the header — this can"
    echo "                                               # make an amplifier markedly louder)"
    echo "    docker restart plum-audio                  # so the picker re-enumerates"
    echo
    echo "  Then pick it in the GUI under Settings -> Audio."
}

main "$@"
