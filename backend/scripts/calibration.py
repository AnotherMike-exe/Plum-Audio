#!/usr/bin/env python3
"""
Plum-Audio — per-endpoint loudness calibration: the curve model and the matching math.

The problem this solves: two endpoints at the same volume percentage are not the same loudness in
the room. A bookshelf pair in a small kitchen and a floorstander in an open living room differ by
speaker sensitivity, amplifier gain, distance and room gain, and none of that is visible to the
protocol. So a user grouping them gets a mix that is wrong in a way no slider position can express.

The fix is empirical: play a tone at a few known volumes, have the user read SPL off a phone meter
from the seat they actually listen from, and fit the endpoint's own volume -> loudness curve. Once
two endpoints each have a curve, "make the kitchen as loud as the living room" becomes arithmetic.

WHY THE FIT IS IN LOG SPACE. A volume percentage scales sample amplitude (see AlsaRenderer's
`_gain` in sendspin_player.py), and SPL is 20*log10(amplitude) + constant. So loudness is linear in
log10(volume), NOT in volume. Plum-Snapcast's version fitted a straight line through (percent, dB)
and was wrong by several dB across the range and divergent near zero, where it extrapolated a
finite loudness for silence. Fitting `dB = a*log10(v) + b` makes `a` a physical quantity: a pure
amplitude scaler gives a == 20, so a fitted `a` far from 20 is evidence of a taper, a compressor,
or a bad measurement -- which is why it is checked rather than trusted.

WHY THE MEASUREMENTS ARE THE WEAK PART. A room has a noise floor. At low volume the meter reads
ambient rather than the speaker, which flattens the low end of the curve, biases `a` downward and
makes the fit over-predict at the top. That is not correctable here -- it is avoided by measuring in
the usable range (see SUGGESTED_SAMPLE_VOLUMES) and by reporting `rms_error` so the GUI can tell the
user their points do not lie on a line. Everything in this module degrades to "not calibrated"
rather than guessing, because a wrong curve is worse than none: it moves a real speaker.

Deliberately dependency-free (no numpy) and side-effect free, so the whole model is unit-testable
with no audio stack, no rig and no event loop -- the same reason player_state.py is its own module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# A volume of 0 is silence, and log10(0) is undefined. Every conversion clamps to this floor rather
# than special-casing zero, so the curve stays total over the whole slider range.
MIN_MODEL_VOLUME = 1.0
MAX_VOLUME = 100.0

# What the wizard proposes. Spread across the usable range and kept clear of both ends: below ~25%
# the room's noise floor contaminates the reading, and at 100% a speaker may already be limiting,
# which puts the point off the line and drags the fit. The user may measure any volumes they like --
# these are only the defaults the wizard pre-fills.
SUGGESTED_SAMPLE_VOLUMES = (35, 60, 85)

MIN_SAMPLES = 2
MAX_SAMPLES = 5

# A pure amplitude scaler gives exactly 20 dB per decade of volume. Real endpoints deviate -- a
# volume taper, a DSP loudness curve or a meter read at the wrong moment all shift it -- but a fit
# outside this band is not a quiet room, it is a bad measurement (or a speaker that was not the one
# making the sound). Outside the band the calibration is rejected rather than applied.
MIN_PLAUSIBLE_SLOPE = 4.0
MAX_PLAUSIBLE_SLOPE = 60.0

# Above this RMS residual the points visibly are not on a line. Not fatal -- the fit is still the
# best available answer -- but the GUI warns, because the usual cause is a measurement taken before
# the meter settled or with music still playing in the room.
FIT_WARN_RMS_DB = 2.5


@dataclass(frozen=True)
class Sample:
    """One measurement: the SPL a user read while this endpoint played the tone at `volume`."""

    volume: float  # endpoint volume percent the tone was played at, 1-100
    db: float  # SPL the user read from the listening position

    def to_dict(self) -> dict:
        return {"volume": self.volume, "db": self.db}

    @staticmethod
    def from_dict(d: dict) -> Sample | None:
        """A sample, or None for anything malformed -- callers drop bad rows, never raise on them."""
        try:
            volume = float(d["volume"])
            db = float(d["db"])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(volume) or not math.isfinite(db):
            return None
        if not 0.0 <= volume <= MAX_VOLUME:
            return None
        return Sample(volume=volume, db=db)


@dataclass(frozen=True)
class Curve:
    """A fitted `dB = a*log10(volume) + b`, plus what the caller needs to distrust it."""

    a: float  # dB per decade of volume; ~20 for a pure amplitude scaler
    b: float  # dB at volume == 1
    n: int  # samples the fit was built from
    rms_error: float  # RMS residual in dB; 0.0 for a two-point (exact) fit

    @property
    def suspect(self) -> bool:
        """True when the points do not lie on a line well enough to trust the extrapolation."""
        return self.rms_error > FIT_WARN_RMS_DB

    def to_dict(self) -> dict:
        return {"a": self.a, "b": self.b, "n": self.n, "rms_error": self.rms_error}


def fit_curve(samples: list[Sample]) -> Curve | None:
    """
    Least-squares fit of `dB = a*log10(v) + b`, or None if the samples cannot support one.

    Returns None -- rather than a degenerate curve -- when there are fewer than two samples, when
    every sample was taken at the same volume (no slope is derivable), or when the resulting slope
    is non-positive or physically implausible. A None here means "not calibrated", which every
    caller already handles by leaving the endpoint alone. That is the safe direction: the failure
    mode of a bad curve is a speaker that jumps to the wrong volume on its own.
    """
    if len(samples) < MIN_SAMPLES:
        return None

    xs = [math.log10(max(s.volume, MIN_MODEL_VOLUME)) for s in samples]
    ys = [s.db for s in samples]
    n = len(xs)

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    # Total sum of squares in x; zero when every measurement was at one volume.
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 1e-12:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))

    a = sxy / sxx
    b = mean_y - a * mean_x

    if not math.isfinite(a) or not math.isfinite(b):
        return None
    if a < MIN_PLAUSIBLE_SLOPE or a > MAX_PLAUSIBLE_SLOPE:
        return None

    residuals = [y - (a * x + b) for x, y in zip(xs, ys, strict=True)]
    rms = math.sqrt(sum(r * r for r in residuals) / n)
    return Curve(a=a, b=b, n=n, rms_error=rms)


def predict_db(curve: Curve, volume: float) -> float:
    """The SPL this endpoint is expected to produce at `volume`."""
    v = min(max(float(volume), MIN_MODEL_VOLUME), MAX_VOLUME)
    return curve.a * math.log10(v) + curve.b


def volume_for_db(curve: Curve, target_db: float) -> float:
    """
    The volume percent that should produce `target_db`, UNCLAMPED except at the model floor.

    Left unclamped on purpose: the caller needs to know the request was out of reach in order to
    report "at limit" rather than silently pretending the endpoint matched. `clamp_volume` is the
    other half.
    """
    exponent = (float(target_db) - curve.b) / curve.a
    # A wildly out-of-range target overflows the exponential before any clamp can catch it.
    if exponent > 6.0:
        return math.inf
    if exponent < -6.0:
        return 0.0
    return math.pow(10.0, exponent)


@dataclass
class MaxLimit:
    """A ceiling on an endpoint, expressed the way the user thinks about it."""

    mode: str = "percentage"  # "percentage" | "decibel"
    value: float = 100.0

    def to_dict(self) -> dict:
        return {"mode": self.mode, "value": self.value}

    @staticmethod
    def from_dict(d: object) -> MaxLimit:
        if not isinstance(d, dict):
            return MaxLimit()
        mode = d.get("mode")
        if mode not in ("percentage", "decibel"):
            mode = "percentage"
        try:
            value = float(d.get("value", 100.0))
        except (TypeError, ValueError):
            value = 100.0
        if not math.isfinite(value):
            value = 100.0
        return MaxLimit(mode=mode, value=value)


@dataclass
class EndpointCalibration:
    """
    One endpoint's stored calibration. Keyed in settings.json by mesh player id (the X25519 peer id
    -- NOT the listener id and not the mDNS name; see CLAUDE.md on the three id namespaces).

    `name` and `url` are denormalised copies for display only. A speaker has two names depending on
    whether it is attached or idle, and its URL moves with DHCP, so neither is safe as a key -- but
    both are worth carrying so the GUI can label a calibration for an endpoint that is currently
    offline.
    """

    name: str = ""
    url: str | None = None
    enabled: bool = True  # participate in loudness matching
    samples: list[Sample] = field(default_factory=list)
    max_limit: MaxLimit = field(default_factory=MaxLimit)
    # Persistent per-room taste, in dB, applied on top of every matched target. This is what keeps
    # "the kitchen is always a little quieter" from being erased by the next re-derive.
    trim_db: float = 0.0
    last_calibrated: str | None = None

    def curve(self) -> Curve | None:
        return fit_curve(self.samples)

    @property
    def calibrated(self) -> bool:
        return self.curve() is not None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "url": self.url,
            "enabled": self.enabled,
            "samples": [s.to_dict() for s in self.samples],
            "maxLimit": self.max_limit.to_dict(),
            "trimDb": self.trim_db,
            "lastCalibrated": self.last_calibrated,
        }

    @staticmethod
    def from_dict(d: object) -> EndpointCalibration:
        """Tolerant by design: a partially-written or older record degrades, it does not raise."""
        if not isinstance(d, dict):
            return EndpointCalibration()
        raw_samples = d.get("samples")
        samples: list[Sample] = []
        if isinstance(raw_samples, list):
            for row in raw_samples[:MAX_SAMPLES]:
                s = Sample.from_dict(row) if isinstance(row, dict) else None
                if s is not None:
                    samples.append(s)
        try:
            trim = float(d.get("trimDb", 0.0))
        except (TypeError, ValueError):
            trim = 0.0
        if not math.isfinite(trim):
            trim = 0.0
        url = d.get("url")
        last = d.get("lastCalibrated")
        return EndpointCalibration(
            name=str(d.get("name") or ""),
            url=url if isinstance(url, str) else None,
            enabled=bool(d.get("enabled", True)),
            samples=samples,
            max_limit=MaxLimit.from_dict(d.get("maxLimit")),
            trim_db=trim,
            last_calibrated=last if isinstance(last, str) else None,
        )


def effective_max_volume(cal: EndpointCalibration) -> float:
    """
    The highest volume percent this endpoint may be driven to.

    In `decibel` mode the ceiling is resolved through the endpoint's own curve, which is the point:
    "no room above 75 dB" is one number the user sets once and every endpoint honours in its own
    units. An uncalibrated endpoint has no curve to resolve it through, so it falls back to 100 --
    a dB ceiling cannot be enforced on an endpoint whose loudness is unknown, and silently
    inventing a percentage would be worse than not enforcing it.
    """
    limit = cal.max_limit
    if limit.mode == "decibel":
        curve = cal.curve()
        if curve is None:
            return MAX_VOLUME
        return _clamp(volume_for_db(curve, limit.value))
    return _clamp(limit.value)


def _clamp(volume: float) -> float:
    if not math.isfinite(volume):
        return MAX_VOLUME if volume > 0 else 0.0
    return min(max(volume, 0.0), MAX_VOLUME)


@dataclass(frozen=True)
class MatchResult:
    """What an endpoint should be set to, and whether it could actually get there."""

    volume: int
    at_limit: bool  # the target was out of reach and the endpoint was clamped to its ceiling
    target_db: float


def target_db_for(cal: EndpointCalibration, volume: float) -> float | None:
    """
    The group target implied by moving THIS endpoint's slider to `volume`.

    The endpoint's own trim is removed, so a room pinned 3 dB quiet does not drag the whole group
    down by 3 dB every time it is the one you happen to touch. Inverse of `match_volume`.
    """
    curve = cal.curve()
    if curve is None:
        return None
    return predict_db(curve, volume) - cal.trim_db


def match_volume(cal: EndpointCalibration, target_db: float) -> MatchResult | None:
    """
    The volume this endpoint must run at to hit `target_db`, with its trim and ceiling applied.

    Returns None when the endpoint has no usable curve or has opted out of matching -- the caller
    leaves such an endpoint entirely alone rather than guessing at a level for it.
    """
    if not cal.enabled:
        return None
    curve = cal.curve()
    if curve is None:
        return None

    wanted_db = float(target_db) + cal.trim_db
    raw = volume_for_db(curve, wanted_db)
    ceiling = effective_max_volume(cal)

    clamped = min(_clamp(raw), ceiling)
    # `raw` above the ceiling is the real "this speaker cannot get there" case. The floor is not
    # reported as a limit: asking for silence and getting silence is not a failure to match.
    at_limit = raw > ceiling + 0.5
    return MatchResult(volume=int(round(clamped)), at_limit=at_limit, target_db=wanted_db)


def load_calibrations(raw: object) -> dict[str, EndpointCalibration]:
    """Parse the `audio.calibration` map out of settings.json, dropping anything unreadable."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, EndpointCalibration] = {}
    for player_id, record in raw.items():
        if isinstance(player_id, str) and player_id:
            out[player_id] = EndpointCalibration.from_dict(record)
    return out


# -- matching scope -----------------------------------------------------------
#
# Calibration answers "how loud is this endpoint"; scope answers the independent question "which
# endpoints are locked to each other". Conflating them would make the kitchen/living-room case the
# only case: adding an office or a laundry room to the same stream would silently drag them into a
# match the user never asked for. So the two are stored and reasoned about separately, and an
# endpoint's curve is useful under every scope.

MATCH_MODES = ("off", "stream", "follow", "sets")
DEFAULT_MATCH_MODE = "follow"


@dataclass
class MatchSet:
    """A named set of endpoints that track each other. Members are mesh player ids."""

    id: str
    name: str = ""
    members: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "members": list(self.members)}

    @staticmethod
    def from_dict(d: object) -> MatchSet | None:
        if not isinstance(d, dict):
            return None
        set_id = d.get("id")
        if not isinstance(set_id, str) or not set_id:
            return None
        raw = d.get("members")
        members = [m for m in raw if isinstance(m, str) and m] if isinstance(raw, list) else []
        return MatchSet(id=set_id, name=str(d.get("name") or ""), members=_dedupe(members))


@dataclass
class MatchPolicy:
    """
    How far loudness matching reaches.

    - ``off``    -- never match; curves are still collected and shown, nothing is driven.
    - ``stream`` -- every calibrated endpoint sharing a stream tracks the one you last moved.
    - ``follow`` -- only endpoints on units already in a follow (slave) relationship. The default,
                    because it acts *only* where the user has already declared two rooms locked
                    together, and needs no second place to configure that.
    - ``sets``   -- explicit named sets, for rooms that should track each other without one of them
                    following the other's source.

    Under every mode except ``off`` an endpoint is only ever matched against endpoints it is
    currently sharing a stream with -- matching across two unrelated streams is meaningless.
    """

    mode: str = DEFAULT_MATCH_MODE
    sets: list[MatchSet] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"mode": self.mode, "sets": [s.to_dict() for s in self.sets]}

    @staticmethod
    def from_dict(d: object) -> MatchPolicy:
        if not isinstance(d, dict):
            return MatchPolicy()
        mode = d.get("mode")
        if mode not in MATCH_MODES:
            mode = DEFAULT_MATCH_MODE
        raw = d.get("sets")
        sets: list[MatchSet] = []
        if isinstance(raw, list):
            for row in raw:
                parsed = MatchSet.from_dict(row)
                if parsed is not None:
                    sets.append(parsed)
        return MatchPolicy(mode=mode, sets=sets)


def _dedupe(items: list[str]) -> list[str]:
    """Order-preserving dedupe -- matching output must not depend on dict iteration order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def matching_partition(
    members: list[str],
    policy: MatchPolicy,
    follow_members: frozenset[str] = frozenset(),
) -> list[tuple[str, list[str]]]:
    """
    Split the endpoints sharing one stream into the subsets that should track each other.

    `members` are the player ids currently in a single group; `follow_members` are the player ids
    belonging to units in an active follow relationship (leader and followers alike), which only
    the ``follow`` mode consults.

    Returns ``(label, members)`` pairs. The LABEL is stable for the life of the grouping — the empty
    string when the mode produces one subset per source, the set id under ``sets`` — so a caller can
    key remembered state on it. Keying on a member id instead would be silently wrong: membership
    order comes from the library's client iteration and changes on any detach, so the key would
    churn and the caller would forget its target and re-baseline. Toning one member is enough to
    trigger it, since that pulls it out of the group.

    Subsets of fewer than two endpoints are dropped: there is nothing to match a lone speaker
    against, and returning it would invite a caller to "match" it to itself and move it for no
    reason. An endpoint appearing in several sets is matched under the first one only, so a
    misconfiguration cannot produce two conflicting targets for the same speaker.
    """
    present = _dedupe([m for m in members if m])
    if policy.mode == "off" or len(present) < 2:
        return []

    if policy.mode == "stream":
        return [("", present)]

    if policy.mode == "follow":
        subset = [m for m in present if m in follow_members]
        return [("", subset)] if len(subset) >= 2 else []

    if policy.mode == "sets":
        claimed: set[str] = set()
        out: list[tuple[str, list[str]]] = []
        for match_set in policy.sets:
            subset = [m for m in match_set.members if m in present and m not in claimed]
            if len(subset) >= 2:
                claimed.update(subset)
                out.append((match_set.id, subset))
        return out

    return []


# The low anchor for a reported dB range. NOT 0: volume 0 is silence, and the model's floor would
# report a finite loudness for it, which the predecessor printed to users as the bottom of the range.
RANGE_LOW_VOLUME = 10.0


def describe(player_id: str, cal: EndpointCalibration) -> dict:
    """The stored record plus everything derived from it, so no client ever refits the curve.

    Lives here rather than in either API module because BOTH serve it — the config API for this
    unit's own records, the mesh API for the merged cross-unit view — and a merged record that
    silently lacked the derived half rendered as "Not calibrated" in the GUI while the matcher was
    happily driving that very speaker.
    """
    payload: dict = {"playerId": player_id, **cal.to_dict()}
    curve = cal.curve()
    payload["calibrated"] = curve is not None
    if curve is None:
        # Distinguish "no measurements yet" from "measurements that do not describe a speaker" —
        # the second is a user error the wizard must explain, not a blank slate.
        payload["curve"] = None
        payload["fitRejected"] = len(cal.samples) >= MIN_SAMPLES
        payload["effectiveMaxVolume"] = effective_max_volume(cal)
        payload["dbRange"] = None
        return payload

    ceiling = effective_max_volume(cal)
    payload["curve"] = {
        "a": curve.a,
        "b": curve.b,
        "n": curve.n,
        "rmsError": curve.rms_error,
        "suspect": curve.suspect,
    }
    payload["fitRejected"] = False
    payload["effectiveMaxVolume"] = ceiling
    payload["dbRange"] = {
        "lowVolume": RANGE_LOW_VOLUME,
        "lowDb": predict_db(curve, RANGE_LOW_VOLUME),
        "highVolume": ceiling,
        "highDb": predict_db(curve, ceiling),
    }
    return payload


def merge_calibrations(sources: list[dict | None]) -> dict[str, EndpointCalibration]:
    """
    Merge several units' stored calibration maps, newest record per endpoint winning.

    Calibration is written by the GUI to the unit SERVING the page (settings.json is owned by that
    unit's Flask process, and a peer's :5002 is deliberately not reachable cross-origin). But the
    endpoint it describes may be grouped on a different unit entirely, and matching runs on whichever
    unit owns the group. Without a merge, "which unit's page you happened to open" would silently
    decide whether matching worked — the kind of difference that reads as a hardware fault.

    So each unit publishes its own map in its snapshot and everyone merges the lot. `lastCalibrated`
    breaks ties, which makes the result identical on every unit and makes a re-calibration on any
    page win everywhere. A record with no timestamp sorts oldest: it predates the field, so anything
    carrying one is newer by definition.

    CAVEAT, and it is a real one: a Pi has no RTC, so this rests on NTP. A unit that has not synced
    stamps 1970 and its records always LOSE; one whose clock has jumped ahead always WINS and pins a
    stale curve across the mesh — and re-calibrating from that unit's page appears to save (the API
    returns the new record) while having no effect on matching. Ties are broken by source order,
    which is stable but arbitrary. Nothing here can detect it; the symptom is a calibration that
    will not take, and the check is `timedatectl` on the units. OPEN-ITEMS.
    """
    best: dict[str, tuple[str, EndpointCalibration]] = {}
    for raw in sources:
        for player_id, cal in load_calibrations(raw).items():
            stamp = cal.last_calibrated or ""
            current = best.get(player_id)
            if current is None or stamp >= current[0]:
                best[player_id] = (stamp, cal)
    return {player_id: cal for player_id, (_, cal) in best.items()}
