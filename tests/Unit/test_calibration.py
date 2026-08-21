"""Unit tests for the loudness-calibration curve model and the matching math.

Pure-logic: no aiosendspin, no audio stack, no rig. Run: `pytest tests/Unit/test_calibration.py`.

The property these tests exist to protect is that a BAD calibration never reaches a speaker. Every
rejection path here (too few points, one volume, a flat or implausible slope, a malformed record)
must return None rather than a degenerate curve, because the failure mode of a wrong curve is a
real speaker jumping to a wrong level on its own -- strictly worse than not matching at all.

The predecessor's version fitted a straight line through (percent, dB). `test_log_model_beats_...`
is the regression that pins why this one does not.
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "scripts"))

from calibration import (  # noqa: E402
    DEFAULT_MATCH_MODE,
    EndpointCalibration,
    MatchPolicy,
    MatchSet,
    MaxLimit,
    Sample,
    effective_max_volume,
    fit_curve,
    highest_rev,
    describe,
    load_calibrations,
    match_volume,
    matching_partition,
    merge_calibrations,
    predict_db,
    target_db_for,
    volume_for_db,
)


def _ideal_samples(offset_db: float, volumes=(35, 60, 85)) -> list[Sample]:
    """Samples from a perfect amplitude scaler (20 dB/decade) sitting `offset_db` from reference."""
    return [Sample(volume=v, db=20.0 * math.log10(v) + offset_db) for v in volumes]


# -- fitting ------------------------------------------------------------------


def test_fits_a_pure_amplitude_scaler_at_20db_per_decade():
    curve = fit_curve(_ideal_samples(30.0))
    assert curve is not None
    assert curve.a == pytest.approx(20.0, abs=1e-6)
    assert curve.b == pytest.approx(30.0, abs=1e-6)
    assert curve.rms_error == pytest.approx(0.0, abs=1e-9)
    assert not curve.suspect


def test_two_points_give_an_exact_fit():
    curve = fit_curve(_ideal_samples(30.0, volumes=(40, 80)))
    assert curve is not None
    assert curve.a == pytest.approx(20.0, abs=1e-6)
    assert curve.rms_error == pytest.approx(0.0, abs=1e-9)


def test_a_doubling_of_volume_costs_about_six_db():
    """The rule of thumb the wizard's help text leans on: 20*log10(2) == 6.02, not 6."""
    curve = fit_curve([Sample(40, 60.0), Sample(80, 66.0)])
    assert curve is not None
    assert curve.a == pytest.approx(19.93, abs=0.01)


def test_five_scattered_points_average_out():
    samples = _ideal_samples(40.0, volumes=(30, 45, 60, 75, 90))
    # Perturb alternately so the noise cancels rather than tilting the line.
    noisy = [Sample(s.volume, s.db + (0.4 if i % 2 else -0.4)) for i, s in enumerate(samples)]
    curve = fit_curve(noisy)
    assert curve is not None
    assert curve.a == pytest.approx(20.0, abs=1.5)
    assert 0.0 < curve.rms_error < 1.0
    assert not curve.suspect


def test_scattered_points_are_flagged_suspect():
    curve = fit_curve([Sample(35, 50.0), Sample(60, 70.0), Sample(85, 52.0)])
    # Either it is rejected outright or it is flagged -- what it must never do is look trustworthy.
    assert curve is None or curve.suspect


# -- rejection paths ----------------------------------------------------------


def test_single_sample_is_not_a_curve():
    assert fit_curve([Sample(50, 60.0)]) is None


def test_no_samples_is_not_a_curve():
    assert fit_curve([]) is None


def test_all_samples_at_one_volume_is_rejected():
    """No slope is derivable, and the old code's divide-by-zero here produced NaN volumes."""
    assert fit_curve([Sample(50, 60.0), Sample(50, 61.0), Sample(50, 59.0)]) is None


def test_flat_response_is_rejected():
    """The predecessor's signature failure: the tone bypassed the volume stage, so every
    measurement read the same SPL. That must be caught, not fitted."""
    assert fit_curve([Sample(38, 62.0), Sample(80, 62.0)]) is None


def test_inverted_response_is_rejected():
    """Louder volume reading quieter means the meter or the speaker was wrong, never the room."""
    assert fit_curve([Sample(35, 70.0), Sample(85, 55.0)]) is None


def test_implausibly_steep_slope_is_rejected():
    assert fit_curve([Sample(35, 10.0), Sample(85, 99.0)]) is None


# -- prediction and inversion -------------------------------------------------


def test_predict_and_invert_round_trip():
    curve = fit_curve(_ideal_samples(35.0))
    for volume in (10, 25, 50, 75, 100):
        db = predict_db(curve, volume)
        assert volume_for_db(curve, db) == pytest.approx(volume, abs=1e-6)


def test_volume_zero_does_not_blow_up_the_model():
    """log10(0) is undefined; the floor keeps the curve total over the whole slider range."""
    curve = fit_curve(_ideal_samples(35.0))
    assert math.isfinite(predict_db(curve, 0))


def test_unreachable_target_returns_infinity_not_an_overflow():
    curve = fit_curve(_ideal_samples(35.0))
    assert volume_for_db(curve, 400.0) == math.inf


def test_log_model_beats_a_linear_fit_in_the_middle():
    """Pins WHY the fit is in log space. Calibrating at 35% and 85% on a real (logarithmic)
    speaker, a linear percent/dB model misreads the midpoint; the log model is exact."""
    low, high = 35, 85
    def true_db(v):
        return 20.0 * math.log10(v) + 30.0

    curve = fit_curve([Sample(low, true_db(low)), Sample(high, true_db(high))])

    mid = 55
    slope = (true_db(high) - true_db(low)) / (high - low)
    linear_prediction = true_db(low) + slope * (mid - low)

    assert abs(predict_db(curve, mid) - true_db(mid)) < 1e-6
    assert abs(linear_prediction - true_db(mid)) > 0.5


# -- max limit ----------------------------------------------------------------


def test_percentage_ceiling_is_taken_literally():
    cal = EndpointCalibration(samples=_ideal_samples(30.0), max_limit=MaxLimit("percentage", 85))
    assert effective_max_volume(cal) == pytest.approx(85.0)


def test_decibel_ceiling_resolves_through_the_endpoints_own_curve():
    cal = EndpointCalibration(samples=_ideal_samples(30.0), max_limit=MaxLimit("decibel", 60.0))
    # 20*log10(v) + 30 == 60  ->  v == 10^1.5 == 31.6
    assert effective_max_volume(cal) == pytest.approx(31.62, abs=0.05)


def test_decibel_ceiling_on_an_uncalibrated_endpoint_falls_back_to_100():
    """A dB ceiling cannot be enforced on an endpoint whose loudness is unknown, and inventing a
    percentage for it would be worse than not enforcing it."""
    cal = EndpointCalibration(samples=[], max_limit=MaxLimit("decibel", 60.0))
    assert effective_max_volume(cal) == 100.0


def test_decibel_ceiling_beyond_reach_clamps_to_100():
    cal = EndpointCalibration(samples=_ideal_samples(30.0), max_limit=MaxLimit("decibel", 200.0))
    assert effective_max_volume(cal) == 100.0


# -- matching -----------------------------------------------------------------


def test_two_identical_endpoints_match_at_the_same_volume():
    """The case most likely to be tested first, and the one the predecessor's guard skipped."""
    a = EndpointCalibration(samples=_ideal_samples(30.0))
    b = EndpointCalibration(samples=_ideal_samples(30.0))
    target = target_db_for(a, 50)
    result = match_volume(b, target)
    assert result is not None
    assert result.volume == 50
    assert not result.at_limit


def test_a_quieter_endpoint_is_driven_louder():
    """The kitchen needs more volume than the living room for the same loudness in the room."""
    living = EndpointCalibration(samples=_ideal_samples(40.0))
    kitchen = EndpointCalibration(samples=_ideal_samples(34.0))  # 6 dB less efficient
    result = match_volume(kitchen, target_db_for(living, 40))
    assert result is not None
    # 6 dB down at 20 dB/decade is exactly a doubling of volume.
    assert result.volume == pytest.approx(80, abs=1)
    assert not result.at_limit


def test_raising_the_reference_raises_the_follower():
    """The behaviour the feature is for: turn the living room up and the kitchen tracks it."""
    living = EndpointCalibration(samples=_ideal_samples(40.0))
    kitchen = EndpointCalibration(samples=_ideal_samples(34.0))
    quiet = match_volume(kitchen, target_db_for(living, 40)).volume
    loud = match_volume(kitchen, target_db_for(living, 60)).volume
    assert loud > quiet


def test_an_endpoint_that_cannot_reach_is_clamped_and_flagged():
    living = EndpointCalibration(samples=_ideal_samples(50.0))
    kitchen = EndpointCalibration(samples=_ideal_samples(20.0))  # far less efficient
    result = match_volume(kitchen, target_db_for(living, 90))
    assert result is not None
    assert result.volume == 100
    assert result.at_limit


def test_clamping_respects_a_percentage_ceiling():
    living = EndpointCalibration(samples=_ideal_samples(40.0))
    kitchen = EndpointCalibration(
        samples=_ideal_samples(40.0), max_limit=MaxLimit("percentage", 60)
    )
    result = match_volume(kitchen, target_db_for(living, 90))
    assert result.volume == 60
    assert result.at_limit


def test_asking_for_silence_is_not_reported_as_a_limit():
    cal = EndpointCalibration(samples=_ideal_samples(30.0))
    result = match_volume(cal, -50.0)
    assert result.volume == 0
    assert not result.at_limit


def test_uncalibrated_endpoints_are_left_alone():
    assert match_volume(EndpointCalibration(samples=[]), 60.0) is None


def test_an_opted_out_endpoint_is_left_alone():
    cal = EndpointCalibration(samples=_ideal_samples(30.0), enabled=False)
    assert match_volume(cal, 60.0) is None


# -- trim ---------------------------------------------------------------------


def test_trim_offsets_the_matched_level():
    living = EndpointCalibration(samples=_ideal_samples(40.0))
    plain = EndpointCalibration(samples=_ideal_samples(40.0))
    quieter = EndpointCalibration(samples=_ideal_samples(40.0), trim_db=-6.0)
    target = target_db_for(living, 80)
    # -6 dB at 20 dB/decade is half the volume.
    assert match_volume(quieter, target).volume == pytest.approx(
        match_volume(plain, target).volume / 2, abs=1
    )


def test_a_trimmed_endpoint_does_not_drag_the_group_when_it_is_the_reference():
    """Removing the trim in `target_db_for` is what stops a room pinned 3 dB quiet from pulling the
    whole house down by 3 dB every time it happens to be the slider you touched."""
    plain = EndpointCalibration(samples=_ideal_samples(40.0))
    trimmed = EndpointCalibration(samples=_ideal_samples(40.0), trim_db=-6.0)
    assert target_db_for(trimmed, 50) == pytest.approx(target_db_for(plain, 50) + 6.0)


def test_trim_survives_a_round_trip_through_the_reference():
    """Move the trimmed endpoint's own slider; it must land back where it was put."""
    trimmed = EndpointCalibration(samples=_ideal_samples(40.0), trim_db=-6.0)
    assert match_volume(trimmed, target_db_for(trimmed, 55)).volume == pytest.approx(55, abs=1)


# -- serialization ------------------------------------------------------------


def test_record_round_trips_through_its_dict_form():
    cal = EndpointCalibration(
        name="Kitchen",
        url="ws://192.168.1.20:8928/sendspin",
        samples=_ideal_samples(30.0),
        max_limit=MaxLimit("decibel", 75.0),
        trim_db=-3.0,
        last_calibrated="2026-08-21T10:00:00Z",
    )
    back = EndpointCalibration.from_dict(cal.to_dict())
    assert back.name == "Kitchen"
    assert back.url == cal.url
    assert back.trim_db == -3.0
    assert back.max_limit.mode == "decibel"
    assert back.max_limit.value == 75.0
    assert len(back.samples) == 3
    assert back.calibrated


def test_malformed_records_degrade_rather_than_raise():
    for junk in (None, [], "nonsense", 42, {"samples": "not-a-list"}):
        cal = EndpointCalibration.from_dict(junk)
        assert not cal.calibrated
        assert cal.trim_db == 0.0


def test_bad_sample_rows_are_dropped_not_fatal():
    cal = EndpointCalibration.from_dict(
        {"samples": [{"volume": 35, "db": 57.0}, {"volume": "x"}, None, {"db": 3}]}
    )
    assert len(cal.samples) == 1


def test_more_than_five_samples_are_truncated():
    rows = [{"volume": v, "db": 20.0 * math.log10(v) + 30.0} for v in (20, 30, 40, 50, 60, 70, 80)]
    assert len(EndpointCalibration.from_dict({"samples": rows}).samples) == 5


def test_load_calibrations_skips_unusable_keys():
    loaded = load_calibrations({"player-a": {"name": "A"}, "": {"name": "empty"}, 7: {}})
    assert list(loaded) == ["player-a"]


def test_load_calibrations_of_junk_is_empty():
    assert load_calibrations("nope") == {}


# -- matching scope -----------------------------------------------------------


def _policy(mode, sets=None):
    return MatchPolicy(mode=mode, sets=sets or [])


def test_off_matches_nothing():
    assert matching_partition(["a", "b", "c"], _policy("off")) == []


def test_stream_mode_locks_everyone_sharing_the_stream():
    assert matching_partition(["a", "b", "c"], _policy("stream")) == [("", ["a", "b", "c"])]


def test_a_lone_endpoint_is_never_matched():
    """Nothing to match against, and 'matching' it to itself would move it for no reason."""
    for mode in ("stream", "follow", "sets"):
        assert matching_partition(["a"], _policy(mode)) == []


def test_follow_mode_only_touches_units_already_slaved_together():
    """The kitchen/living-room case: they are locked because the user already said so."""
    members = ["kitchen", "living", "office"]
    assert matching_partition(members, _policy("follow"), frozenset({"kitchen", "living"})) == [
        ("", ["kitchen", "living"])
    ]


def test_follow_mode_is_inert_without_a_follow_relationship():
    assert matching_partition(["a", "b"], _policy("follow"), frozenset()) == []


def test_sets_mode_keeps_independent_rooms_independent():
    """Adding the office to the same stream must not drag it into the kitchen's match."""
    policy = _policy(
        "sets",
        [
            MatchSet(id="s1", name="Open plan", members=["kitchen", "living"]),
            MatchSet(id="s2", name="Upstairs", members=["office", "laundry"]),
        ],
    )
    members = ["kitchen", "living", "office", "laundry", "garage"]
    assert matching_partition(members, policy) == [("s1", ["kitchen", "living"]), ("s2", ["office", "laundry"])]


def test_sets_mode_ignores_members_not_on_the_stream():
    policy = _policy("sets", [MatchSet(id="s1", members=["kitchen", "living", "absent"])])
    assert matching_partition(["kitchen", "living"], policy) == [("s1", ["kitchen", "living"])]


def test_a_set_with_one_member_present_is_dropped():
    policy = _policy("sets", [MatchSet(id="s1", members=["kitchen", "living"])])
    assert matching_partition(["kitchen", "office"], policy) == []


def test_an_endpoint_in_two_sets_is_claimed_by_the_first_only():
    """A misconfiguration must not produce two conflicting targets for one speaker."""
    policy = _policy(
        "sets",
        [
            MatchSet(id="s1", members=["a", "b"]),
            MatchSet(id="s2", members=["b", "c"]),
        ],
    )
    assert matching_partition(["a", "b", "c"], policy) == [("s1", ["a", "b"])]


def test_partition_is_order_stable_and_deduped():
    assert matching_partition(["a", "b", "a", "", "b"], _policy("stream")) == [("", ["a", "b"])]


def test_policy_round_trips():
    policy = MatchPolicy(mode="sets", sets=[MatchSet(id="s1", name="Open plan", members=["a", "b"])])
    back = MatchPolicy.from_dict(policy.to_dict())
    assert back.mode == "sets"
    assert back.sets[0].name == "Open plan"
    assert back.sets[0].members == ["a", "b"]


def test_unknown_mode_falls_back_to_the_default():
    assert MatchPolicy.from_dict({"mode": "nonsense"}).mode == DEFAULT_MATCH_MODE
    assert MatchPolicy.from_dict(None).mode == DEFAULT_MATCH_MODE


def test_malformed_sets_are_dropped_not_fatal():
    policy = MatchPolicy.from_dict({"mode": "sets", "sets": [{"name": "no id"}, None, {"id": "ok"}]})
    assert [s.id for s in policy.sets] == ["ok"]


# -- merging across units -----------------------------------------------------


def _record(offset_db, stamp):
    return {
        "samples": [{"volume": v, "db": 20.0 * math.log10(v) + offset_db} for v in (35, 85)],
        "lastCalibrated": stamp,
    }


def test_merge_prefers_the_newest_record_for_an_endpoint():
    """A re-calibration on ANY unit's page must win everywhere, identically on every unit."""
    older = {"spk": _record(30.0, "2026-08-01T00:00:00Z")}
    newer = {"spk": _record(40.0, "2026-08-20T00:00:00Z")}
    for order in ([older, newer], [newer, older]):
        merged = merge_calibrations(order)
        assert merged["spk"].last_calibrated == "2026-08-20T00:00:00Z"


def test_merge_unions_endpoints_across_units():
    merged = merge_calibrations([{"a": _record(30.0, "t1")}, {"b": _record(30.0, "t1")}])
    assert set(merged) == {"a", "b"}


def test_merge_sorts_an_undated_record_oldest():
    """No timestamp means the record predates the field, so anything dated is newer by definition."""
    undated = {"spk": {"samples": [{"volume": 35, "db": 50}, {"volume": 85, "db": 58}]}}
    dated = {"spk": _record(40.0, "2026-08-20T00:00:00Z")}
    assert merge_calibrations([dated, undated])["spk"].last_calibrated == "2026-08-20T00:00:00Z"
    assert merge_calibrations([undated, dated])["spk"].last_calibrated == "2026-08-20T00:00:00Z"


def test_merge_tolerates_empty_and_missing_maps():
    assert merge_calibrations([None, {}, "junk"]) == {}


def test_the_partition_label_is_stable_across_a_membership_change():
    """The label keys a caller's remembered target, so it must not churn when a member leaves.
    Keying on a member id instead would forget the group's level every time one was toned."""
    policy = _policy("stream")
    before = matching_partition(["a", "b", "c"], policy)[0][0]
    after = matching_partition(["b", "c"], policy)[0][0]
    assert before == after == ""


def test_sets_labels_are_the_set_ids():
    policy = _policy("sets", [MatchSet(id="upstairs", members=["a", "b"])])
    assert matching_partition(["a", "b"], policy)[0][0] == "upstairs"


# -- describe() ---------------------------------------------------------------


def test_describe_carries_the_derived_half():
    """Both API surfaces serve this. A record without it renders as "Not calibrated" in the GUI
    while the matcher is driving that very speaker."""
    cal = EndpointCalibration(name="Kitchen", samples=_ideal_samples(30.0))
    payload = describe("kitchen", cal)
    assert payload["playerId"] == "kitchen"
    assert payload["calibrated"] is True
    assert payload["curve"]["a"] == pytest.approx(20.0, abs=1e-6)
    assert payload["effectiveMaxVolume"] == 100.0
    assert payload["dbRange"]["lowVolume"] == 10.0
    assert payload["fitRejected"] is False


def test_describe_separates_no_measurements_from_rejected_ones():
    blank = describe("a", EndpointCalibration())
    assert blank["calibrated"] is False and blank["fitRejected"] is False
    flat = describe("b", EndpointCalibration(samples=[Sample(38, 62.0), Sample(80, 62.0)]))
    assert flat["calibrated"] is False and flat["fitRejected"] is True


# -- causal ordering, not clock ordering --------------------------------------
#
# A Pi has no RTC, so a wall-clock comparison rests entirely on NTP. The failure is silent and
# nasty: a unit whose clock jumped ahead pins a stale curve mesh-wide, and re-calibrating from the
# affected page appears to save while never taking effect.


def _rev_record(offset_db, rev, stamp):
    return {
        "samples": [{"volume": v, "db": 20.0 * math.log10(v) + offset_db} for v in (35, 85)],
        "lastCalibrated": stamp,
        "rev": rev,
    }


def test_a_higher_rev_wins_against_a_future_dated_clock():
    """The scenario the field exists for: unit B's clock is years ahead, but unit A holds the
    genuinely newer record."""
    stale_but_future = {"spk": _rev_record(30.0, rev=1, stamp="2031-01-01T00:00:00Z")}
    fresh_but_behind = {"spk": _rev_record(40.0, rev=2, stamp="2026-08-21T00:00:00Z")}
    for order in ([stale_but_future, fresh_but_behind], [fresh_but_behind, stale_but_future]):
        assert merge_calibrations(order)["spk"].rev == 2


def test_a_higher_rev_wins_against_an_unsynced_1970_clock():
    """The other direction: a unit that has not synced stamps the epoch, and must still win if its
    record is causally later."""
    epoch_newer = {"spk": _rev_record(40.0, rev=5, stamp="1970-01-01T00:00:00Z")}
    dated_older = {"spk": _rev_record(30.0, rev=4, stamp="2026-08-21T00:00:00Z")}
    assert merge_calibrations([dated_older, epoch_newer])["spk"].rev == 5


def test_the_timestamp_still_breaks_a_rev_tie():
    """Only reachable for records written before `rev` existed — they all carry 0."""
    old = {"spk": _rev_record(30.0, rev=0, stamp="2026-08-01T00:00:00Z")}
    new = {"spk": _rev_record(40.0, rev=0, stamp="2026-08-20T00:00:00Z")}
    assert merge_calibrations([old, new])["spk"].last_calibrated == "2026-08-20T00:00:00Z"
    assert merge_calibrations([new, old])["spk"].last_calibrated == "2026-08-20T00:00:00Z"


def test_a_record_predating_rev_loses_to_any_saved_since():
    """Migration: existing records have no `rev`, so they read as 0 and the first re-save wins."""
    legacy = {"spk": {"samples": [{"volume": 35, "db": 60}, {"volume": 85, "db": 68}]}}
    saved_since = {"spk": _rev_record(40.0, rev=1, stamp="1970-01-01T00:00:00Z")}
    assert merge_calibrations([legacy, saved_since])["spk"].rev == 1


def test_highest_rev_reads_the_high_water_mark():
    a = {"spk": _rev_record(30.0, rev=3, stamp="t"), "other": _rev_record(30.0, rev=9, stamp="t")}
    b = {"spk": _rev_record(30.0, rev=7, stamp="t")}
    assert highest_rev([a, b], "spk") == 7
    assert highest_rev([a, b], "absent") == 0
    assert highest_rev([None, "junk"], "spk") == 0


def test_rev_round_trips_and_is_never_negative():
    assert EndpointCalibration.from_dict({"rev": 4}).rev == 4
    assert EndpointCalibration.from_dict({"rev": -2}).rev == 0
    assert EndpointCalibration.from_dict({"rev": "nonsense"}).rev == 0
    assert EndpointCalibration(rev=3).to_dict()["rev"] == 3
