# Volume Calibration and Loudness Matching

> Measure how loud each endpoint actually is in its room, then hold grouped rooms level with each
> other. Ported in concept from Plum-Snapcast, where it was built but never tested and could not
> have worked — see [What the predecessor got wrong](#what-the-predecessor-got-wrong).
>
> **Status**: implemented, unit-tested, **not yet hardware-validated**. See
> [Rig validation checklist](#rig-validation-checklist).

## The problem

Two endpoints at the same volume percentage are not the same loudness. Speaker sensitivity, amp
gain, listening distance and room gain all differ, and none of it is visible to the protocol. So a
user who groups the kitchen with the living room gets a mix that is wrong in a way no single slider
position can express.

The fix is empirical. Play a known signal from one endpoint at a few known volumes, have the user
read SPL off a phone meter from the seat they actually listen from, and fit that endpoint's own
volume → loudness curve. Once two endpoints each have a curve, "make the kitchen as loud as the
living room" is arithmetic.

## The model

Fit is in **log space**:

```
dB = a · log10(volume) + b
```

A volume percentage scales sample amplitude (`AlsaRenderer._gain`, `sendspin_player.py`), and SPL is
`20·log10(amplitude) + constant` — so loudness is linear in `log10(volume)`, **not** in volume. That
makes `a` a physical quantity: a pure amplitude scaler gives `a == 20`, so a fitted slope far from
20 is evidence of a taper, a compressor, or a bad measurement. It is checked, not trusted
(`MIN_PLAUSIBLE_SLOPE` / `MAX_PLAUSIBLE_SLOPE`).

Two to five measurements. Two give an exact line; more are least-squares fitted and the RMS residual
is reported so the GUI can say "these points are not on a line" (`FIT_WARN_RMS_DB`).

**Everything derived is computed server-side** — the curve, `calibrated`, `effectiveMaxVolume`, the
dB range — and sent to the GUI. There is exactly one implementation of the maths, so the tab can
never disagree with the matcher about how loud a room is.

### Matching

A group has a transient target loudness `T`. Endpoint *i* renders at:

```
volume_i = invert_i(T + trim_i)      clamped to endpoint i's ceiling
```

Moving **any** endpoint's slider sets `T` from that endpoint's own curve, with its trim removed
first, and every other member re-derives:

```
T = predict_i(volume_i) − trim_i
```

Removing the trim is load-bearing: without it, a room pinned 3 dB quiet would drag the whole house
down by 3 dB every time it happened to be the slider you touched.

`trim_i` is **persistent per-room taste**, edited in the calibration UI rather than by dragging.
That is what lets "the kitchen is always a little quieter" survive every re-derive.

### Caps

An endpoint that cannot reach the target is clamped to its ceiling and flagged `at_limit`; the rest
of the group stays correctly matched. One weak speaker never drags the whole house down. The GUI
badges that row.

The ceiling is either a percentage or an **absolute dB**, resolved through that endpoint's own
curve — so "no room above 75 dB" is one number the user sets once and every endpoint honours in its
own units. On an uncalibrated endpoint a dB ceiling falls back to 100%: a loudness limit cannot be
enforced on a speaker whose loudness is unknown, and inventing a percentage would be worse.

## Scope — which endpoints lock together

A separate question from calibration, and stored separately (`audio.loudnessMatch`). A curve says
how loud one endpoint is; the scope says which endpoints are locked to each other. Conflating them
would make the kitchen/living-room case the only case — adding an office to the same stream would
silently drag it into a match nobody asked for.

| Mode | Behaviour |
|---|---|
| `off` | Curves are kept and shown; nothing is ever driven. |
| `follow` | **Default.** Only endpoints on units already slaved together under Playback → Follow. Acts exactly where the user has declared two rooms locked, and needs no second place to configure it. **Never matches a third-party speaker** — see below. |
| `stream` | Every calibrated endpoint sharing a stream tracks the one last moved. |
| `sets` | Explicit named groups, for rooms that should track each other without one following the other's source. |

Under every mode but `off`, endpoints are only ever matched against endpoints they are **currently
sharing a stream with** — matching across two unrelated streams is meaningless. An endpoint listed
in several sets is claimed by the first, so a misconfiguration cannot produce two conflicting
targets for one speaker.

`follow` mode needs mesh-wide knowledge of follow relationships, but follow config lives on the
**follower**. So `UnitSnapshot.follows_unit_id` is published, written by `FollowReconciler` (which
already reads the setting every tick). Only each unit's **own** speaker counts, read from its
`local_player` self-report — a unit's `players` list is every client attached to its server, which
after a roam includes speakers belonging to units that follow nobody.

## Third-party (non-Plum) endpoints

Records are keyed by mesh `player_id` and stored in a Plum unit's `settings.json`, so a foreign
speaker's curve lives on our side by construction. The identity is sound too, and in one respect
better than a Plum player's: an adopted speaker's `player_id` is its **handshake client id**
(`sendspin_server.py:1567`), typically MAC-derived, which survives reconnect, reboot and a DHCP move
— where an X25519 peer id dies with `/config/identity`.

### Two kinds, and how each is reached

**Already adopted onto one of our sources.** `snapshot()` applies no ownership filter
(`sendspin_server.py:1547-1553`), so it appears in `unit.players` like any other endpoint. The list
shows it, `route_player` takes its intra-server path, and `set_player_volume` reaches it — a
cleartext client is activated straight from its negotiated role set, so no pairing is involved.

**Idle and visible only over mDNS.** It is in no unit's `players` and no unit's `local_player`, so
`Router.route_player` cannot resolve it at all and the mesh view does not carry it.
`calibrationService.getEndpoints()` therefore also reads `GET /api/mesh/neighbourhood` — a separate
surface covering everything mDNS can see that is not a Plum unit — and lists such a speaker with its
**listener URL as a provisional id**. Starting the tone then **adopts** it, and the reply carries the
id its handshake gave. The GUI keys the record on that, and the wizard refuses to save until it has
one: a URL is IP-derived, so a curve stored against one is orphaned by the next DHCP lease.

Stopping the tone **hands an adopted speaker back** with `release_foreign_client` — detach,
`_stop_dialing`, `conn.disconnect()` — not `unroute_player`, which drops it from the group and
leaves the websocket up. A client holds exactly one, so the speaker would stay captured by us and
Music Assistant could never take it back.

### What stays true regardless

1. **A server cannot READ a speaker's volume, only command it.** Our own player echoes
   `client/state` after every change because we made it do so (`SPEC-CONFORMANCE.md`); a third-party
   speaker reports its connect-time level and never moves. Two consequences, both handled:
   - The matcher's `_unconfirmed` guard means a commanded endpoint is not readable as user intent
     until its echo confirms, so a frozen level cannot masquerade as a slider drag.
   - The tone does **not** restore a third-party speaker's level, because it never read one.
     `_current_placement` only trusts a reported volume for an endpoint that is some unit's own
     player. The wizard says so, rather than leaving it as a surprise.
2. **`follow` scope excludes them by construction.** `_follow_members` builds from each unit's
   `local_player`, and a foreign speaker is in no unit's. That is *mostly* right — `follow` means
   unit-to-unit source slaving and a speaker with no unit participates in none — but it drops a real
   case: a foreign speaker adopted onto the leader's source, in the same room as the leader's own
   speaker, is exactly what this feature is for. The GUI badges such a row **"Not matched under
   Follow"** and the scope blurb points at `sets`.
3. **Never stage a pairing PSK against one.** Staging turns the next handshake into a pairing
   handshake, which the library aborts against a cleartext client, taking it offline until the next
   adopt. `reclaim_remote_player` used to stage unconditionally on the strength of a comment
   claiming its `player_id` "only ever names a Plum player" — false, since `snapshot()` has no
   ownership filter. `Router._may_stage_pairing` now requires positive evidence (the id is some
   unit's own speaker, or the holder reported a non-None `security`), and the ambiguous case fails
   safe. OPEN-ITEMS #21.

## The tone

`backend/scripts/calibration_tone.py`. The tone is a real, transient Sendspin source
(`cal:<player_id>`) with the target player alone in its group.

**It has to be.** The measurement is of the endpoint's own gain stage, so the tone must travel the
same path the music does: ingest → encode → websocket → jitter buffer → the player's volume multiply
→ the DAC. That also makes it work uniformly for this unit's own speaker, a peer's speaker (the
router's cross-server reclaim) and an adopted third-party speaker — none of which a local ALSA write
could reach. `audio_devices.test_device` is no help either: it deliberately refuses the card the
player already holds, which is precisely the card being calibrated.

- **Pink noise by default.** A single sine at one listening position sits in whatever standing-wave
  pattern the room has at that frequency, so moving the meter a foot can change the reading by more
  than the effect being measured. A 1 kHz sine is offered because it is easier to hear as "still
  playing", not because it measures better.
- **Deterministic** (fixed seed). Every sample in a calibration — and every endpoint in the mesh —
  must hear an identically-shaped signal, or the differences being measured are partly differences
  in the noise.
- **Voss-McCartney**, pure Python, generated in an executor thread. The audio process does not
  import numpy and adding it to make a test tone would be a poor trade; the loop is long enough to
  stall the feeder's 20 ms commit cadence on a Pi, hence the thread.
- **It routes the endpoint**, pulling it off whatever it was playing. The session records where the
  speaker was and what level it was at, and restores both on stop.
- **It expires on its own** (`MAX_TONE_SECONDS`). A browser that navigates away cannot press Stop.
- **Re-levelling does not restart it** — restarting gaps the noise while the meter is integrating,
  which is exactly when a reading goes wrong.

The tone source stays **visible in the mesh snapshot** deliberately: `Router.route_player` resolves
a source through the view, so hiding it there would make a peer's speaker impossible to tone. The
GUI filters it by the `cal:` prefix instead (`CALIBRATION_SOURCE_PREFIX`).

## Where the pieces run

| Concern | Process | Why |
|---|---|---|
| Curve CRUD (`/api/audio/calibration`) | Flask config API, **:5002** | Persistence only. Cannot make a sound, does not try. |
| The tone (`/api/mesh/calibration/tone`) | aiohttp mesh API, **:5001** | Only the audio loop can create a source and route a player. |
| Merged read (`/api/mesh/calibration`) | aiohttp mesh API, **:5001** | Unions every unit's records; see below. |
| Matching (`LoudnessReconciler`) | audio event loop | Polls the view; drives `Router.set_volume`. |

**Read merged, write local.** A record can only be written to the unit *serving the page* — a peer's
:5002 is deliberately not reachable cross-origin (`apis/server.py`) — but the endpoint it describes
may be grouped on a different unit entirely, and matching runs on whichever unit owns the group. So
each unit publishes its own map in `UnitSnapshot.calibration` and everyone merges the lot
(`calibration.merge_calibrations`). Without that, *which unit's page you happened to open* would
silently decide whether matching worked.

Duplicates are ordered by a causal `rev`, **not by the clock**. A Pi has no RTC, so a timestamp
comparison rests entirely on NTP — and fails silently in the worst way: a unit whose clock jumped
ahead pins a stale curve mesh-wide, and re-calibrating from the affected page appears to save while
never taking effect. A save stores `max(highest rev the client has seen anywhere, the local rev) + 1`,
allocated inside the settings lock. The browser supplies the high-water mark because it is the only
party holding the merged view; that is safe, because the value can only push the stored rev higher.
`lastCalibrated` remains for display, and as a tiebreak for records written before `rev` existed.

### Why a polling reconciler

Same reasons as `FollowReconciler`: the audio hot path stays untouched, membership changes and
volume changes are handled by one mechanism rather than two, `tick()` is directly unit-testable, and
the whole thing degrades to a no-op for uncalibrated endpoints.

**"The user moved this one" is detected by divergence.** There is no event for it. The volume POST
can land on any unit, and routing and follow both happen with no browser open, so a GUI-side
implementation would be wrong in several directions at once. The reconciler remembers the volume it
last *commanded* per player and watches for the view reporting something else — that divergence is a
human. Cost: up to one poll interval (~2 s) of settle after the slider is released.

It runs on the unit that owns the **source**, over that source's own group members. That is not
arbitrary: `set_player_volume` resolves a client on the local server, so a unit can only drive the
players attached to its own groups — exactly `SourceState.player_ids`. Every member is therefore
driven by exactly one unit, and two reconcilers can never fight over one speaker.

### The deliberate exception to "do not fan out per client"

CLAUDE.md says group volume belongs to the library's delta-preserving redistribution and must not be
reimplemented per client. That rule is about the **group slider**, which is unchanged. This is a
different quantity: a per-endpoint correction the protocol has no concept of, which by definition
cannot be expressed as one group level. It is confined to groups whose members are calibrated *and*
in scope, so a mesh with no calibration behaves identically to one without this feature.

## Guards worth knowing

- **A rejected fit never reaches a speaker.** Fewer than two samples, all at one volume, a flat or
  inverted response, or an implausible slope all return `None` — which every caller already handles
  as "not calibrated". The failure mode of a bad curve is a real speaker jumping to a wrong level on
  its own; refusing is strictly safer. The API turns it into a 400 with an explanation, because the
  wizard has to tell the user *why* at the moment they press Save.
- **First sight of a group moves nothing.** The reconciler adopts the state the user left rather
  than picking a reference and imposing one.
- **Muting one room does not mute the house** (`MIN_REFERENCE_VOLUME`).
- **The endpoint playing a calibration tone is never treated as a reference.** Its level is
  deliberately unrelated to the group's; reading it as a slider move would re-level the whole house
  mid-measurement.
- **Writes go through `SettingsManager.mutate`**, which holds the lock across the whole
  read-modify-write. The map is keyed by id, so a get-then-post would let two browsers calibrating
  two speakers each read the same map and the second drop the first — *with a bumped version*, so no
  poller would ever reconcile it.
- **A previous calibration source is never restored onto.** That would leave a speaker attached to a
  group whose feeder has been torn down.

## What the predecessor got wrong

Plum-Snapcast's version was built and never tested. Three defects meant it could not have worked,
and they are the reason this is a rebuild rather than a port:

1. **The tone bypassed the volume stage.** `sox -n -t alsa default` wrote straight to the server's
   own ALSA device; `Client.SetVolume` attenuates inside snapclient, which the tone never passed
   through. Both measurements would read the same SPL → slope 0 → divide-by-zero → `NaN` sent as a
   client volume. The dead `CALIBRATION_FIFO` / `CALIBRATION_STREAM_NAME` constants show the correct
   design was planned and abandoned.
2. **Persistence was never wired.** `settingsService.updateSettings` forwarded an explicit key
   whitelist that omitted `audio`, so every calibration was silently dropped on write and absent on
   read. `saveCalibration` returned `true` while writing nothing.
3. **`sox` was not in the image** — only the `soxr` resampler *library*. Every tone request would
   500, and the frontend never checked `response.ok`, so the Play button just did nothing.

Also: the fit was linear in percent (physically wrong, and it extrapolated a finite loudness for
silence and printed it as the bottom of the range); the max limit was never enforced anywhere; the
slider/hardware conversion was applied in the wrong direction on both ends of the matching path; and
the "skip if the matched volume equals the reference volume" guard skipped exactly the
identical-speakers case, which is the first thing anyone would test.

What survived: the wizard concept, the two-mode max limit, and the settings key.

## Rig validation checklist

Nothing below is proven on hardware yet.

1. **The tone is audible from exactly one endpoint**, and only that one. Other members of a group
   keep playing their music.
2. **The endpoint's volume actually changes the tone's SPL.** This is defect #1 above; measure 35%
   and 85% and confirm the readings differ by roughly `20·log10(85/35) ≈ 7.7 dB`. If they are equal,
   the tone is not passing through the gain stage and nothing downstream can be trusted.
3. **Stop restores** the endpoint to its previous source and level.
4. **Abandoning the wizard** (close the tab) stops the tone within `MAX_TONE_SECONDS` and restores.
5. **Toning a peer's speaker** works from another unit's GUI (cross-server reclaim path).
6. **Toning an idle speaker** works (the router's idle-player fallback).
7. **Matching**: calibrate two rooms, group them, move one slider, confirm the other tracks within
   ~2 s and that measured SPL at the two listening positions agrees within ~1–2 dB.
8. **A capped endpoint** badges "At limit" and does not drag the reference down.
9. **A trim survives** a re-level.
10. **Scope**: with `follow`, an office joined by hand to the same stream is left alone.
11. **Cross-unit records**: calibrate from unit A's GUI, confirm unit B's matcher uses it.
12. **The `cal:` source never appears** in any GUI stream list or picker, on any unit.

### Third-party endpoints

13. **An idle mDNS-only speaker is listed** under Settings → Audio → Calibration, named as it calls
    itself rather than by its mDNS instance name.
14. **Playing the tone adopts it**, the wizard picks up its handshake id, and Save is refused until
    it has one.
15. **Stop hands it back** — confirm Music Assistant can take it again without a power cycle.
16. **Its level is not "restored"** to some connect-time value on stop.
17. **A normal Plum-to-Plum roam still pairs** and does not land silent (the `stage_pairing` guard —
    this is the regression risk of OPEN-ITEMS #21's fix, and it wants the `.7` pair).
18. **Cross-routing an adopted ESP32 between two units does not take it offline** (the same guard,
    from the other side). Signature of the old bug: the NEXT adopt succeeds.

## Files

| Concern | File |
|---|---|
| Curve model, scope policy, merge | `backend/scripts/calibration.py` |
| Tone synthesis + session lifecycle | `backend/scripts/calibration_tone.py` |
| Curve CRUD (:5002) | `backend/scripts/apis/calibration_api.py` |
| Tone + merged read routes (:5001) | `backend/scripts/mesh/api.py` |
| The matcher | `backend/scripts/mesh/loudness.py` |
| Locked read-modify-write | `SettingsManager.mutate`, `backend/scripts/apis/settings_api.py` |
| Follow topology publishing | `backend/scripts/mesh/follow.py`, `mesh/model.py` |
| API client | `frontend/services/calibrationService.ts` |
| Endpoint list + scope UI | `frontend/components/settings/CalibrationSection.tsx` |
| Measurement UI | `frontend/components/settings/CalibrationWizard.tsx` |
| Tests | `tests/Unit/test_calibration{,_api,_tone}.py`, `tests/Unit/test_loudness_reconciler.py`, `frontend/tests/unit/services/calibrationService.test.ts` |
