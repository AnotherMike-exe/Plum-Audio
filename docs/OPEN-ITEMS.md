# Open items — known gaps, deferred calls, and things not yet chased

> **Purpose**: the running list of what is known-broken, known-missing, or deliberately deferred.
> Split out of `docs/CLAUDE.md` on 2026-08-13, because none of it is a *rule* — an agent does not
> need it loaded to avoid breaking something, and it was half that file's length.
>
> `docs/CLAUDE.md` keeps only the handful of open items that change how you would write code today,
> with a pointer here. Everything else lives below. Resolved items are kept, struck through, with
> what actually fixed them — that history is the point, and several entries below were re-opened or
> overturned once someone tested them.


1. **DLNA and Plexamp have no backend.** Established by *watching the running GUI* on `.100.21`
   (2026-08-06) after two wrong descriptions here — a truncated `grep | head -20` produced the
   second, so **read the whole grep**:
   - `IntegrationsTab.tsx` **does** contain full DLNA (`:1250`) and Plexamp (`:1433`) sections,
     ~330 lines with handlers and CRUD.
   - They do not RENDER: `Settings.tsx:43` passes `enabledSources={['airplay','spotify','bluetooth']}`
     and `show()` gates both out.
   - But `loadDlnaEndpoints()` ran on mount regardless of that gate, so every open of the
     Integrations tab logged two console errors against `/api/integrations/dlna/endpoints` — a route
     `create_integrations_blueprint` does not register. **Fixed 2026-08-06**: the effect now returns
     early when the section is hidden.
   Remaining scaffolding: the two card bodies, `types.ts`'s `DLNAEndpoint`, and `settings_api.py`'s
   `integrations.dlna`/`.plexamp` defaults (Plexamp's gated on `PLEXAMP_ENABLED`).
2. **Four frontend test suites assert nothing about production code** (`NowPlaying`,
   `PlayerControls`, `integrationsService`, `settingsService`). `PlayerControls` now has a real
   counterpart beside it (`PlayerControlsSourceVolume`); the other three do not. See TESTING.md.
3. ~~`sendspin_server.py` has no unit coverage~~ — **done 2026-08-06**,
   `tests/Unit/test_sendspin_server.py` (29 tests). `refresh_stream` and its `attach_player` caller
   are pinned in call order against fakes; deleting the refresh fails two tests. Also covers the
   `_primary_source` handoff, controller grouping and source lifecycle.
4. **`configure-audio-hat.sh`'s no-`dtoverlay` fallback has never run on real hardware** — unit-tested
   against fixtures only. `--keep-onboard` HAS now run, on `.100.21` (2026-08-05), and had two
   independent bugs that fixtures could not have caught: it left an out-of-block `dtparam=audio=off`
   armed, and omitting `audio=off` is not the same as asking for `audio=on` (the firmware default is
   off). Both fixed and verified across a reboot.
5. ~~Visualizer, About and Integrations have no visual review under a live stream~~ — **DONE
   2026-08-06**, in-browser on `.100.21` (idle) and `.2.10` (live Spotify). Everything renders:
   artwork, metadata, progress, both volume sliders, shuffle/repeat shown only because Spotify
   advertises them, and a track change updating metadata + artwork live. The **visualizer is
   audio-reactive under a live stream** — successive frames show different spectra — and **album-art
   theming works**, re-colouring the whole UI from the artwork with contrast preserved. It is
   opt-in: Settings → Theme → *Album Art Colors*, off by default, per-browser. Left OFF as found.
   Two real defects were found and fixed: the About tab was unported from Plum-Snapcast wholesale,
   and the DLNA console errors in item 1. Console is otherwise clean.
6. ~~**Multi-server arbitration** is a spec MUST we only half-implement~~ — **CLOSED 2026-08-13 by
   the 9.1.0 bump.** The library now arbitrates internally: `attach_websocket` brings a connection up
   provisionally, handshakes, then admits or rejects by `Activity` rank with a persisted
   `last_playback_server_id` as the tiebreak. Our yield-to-newest workaround is deleted. No policy
   hook, and it keys on `Activity` rather than the `connection_reason` we drive.
   ~~**Replaced by a new standing deviation: we implement NO pairing method**~~ — **that deviation is
   CLOSED too, 2026-08-13.** All three methods are implemented and reachable from the GUI, a unit
   pairs with its own speaker automatically, peers pair via `PLUM_FLEET_PSK`, and unpaired access
   defaults off. What remains is narrower and external: `PLUM_ALLOW_UNENCRYPTED=1` keeps the non-spec
   cleartext path open, because `sendspin-cpp` has no Noise in any release and our own web GUI is a
   hand-rolled cleartext client on :8927. Those are one change, not three.
   `docs/SENDSPIN-PAIRING.md`.
   **Still open under this heading: the FIRST roam to a never-paired peer is slow, and Music Assistant
   contends for the player.** Measured on `.7.204`'s player, 2026-08-13. `reclaim_remote_player` gives
   up at 10 s and reports failure; the player actually lands ~30 s later and the route completes, so
   the GUI shows an error for a roam that then works. B's player log across that window shows `.122`
   dialling five times with 1/2/4/8/12 s backoff, each attach dropping — interleaved with **two dials
   from Music Assistant on `.7.226`**, which is a third server legitimately competing for the same
   single websocket. Every subsequent roam is 3/3 in ~13 s, so this is a cold-start cost, not a
   steady-state one. Not yet separated: how much is first-pairing and how much is MA contention. The
   obvious next step is to re-measure with MA stopped before touching any timeout.
   **Refined 2026-08-13, same evening:** first-pairing is NOT the cost. Under live audio A's player
   was dialled at 15:21:57.879 and paired at 15:21:58.047 — **170 ms, inside the handshake**, on a
   pairing it had never done before. So the 30 s belongs to contention (or to the pre-staging build),
   not to pairing. Measure with MA stopped.
   ~~**The roam failure is DIRECTIONAL**~~ — **CLOSED 2026-08-13: a leaked `management` session.**
   `open_pairing_window` called `enable_management()` and never disabled it. A declared activity is
   part of what the client's arbitration ranks when a second server dials, so a server still holding
   `management` outranks a peer asking for plain PLAYBACK: the peer's dial is accepted provisionally,
   handshakes, and is then rejected — it lands in the peer's registry as `(disconnected)` and the
   reclaim polls 10 s for a client that never comes up. Nothing expired the session, so that player
   could not be roamed for the rest of the process's lifetime, and a restart "fixed" it.

   It looked directional only because `.122` had had a pairing window opened on its player and `.204`
   had not. The reproduction is exact: **12/12 successful roams, ONE `/api/mesh/pairing-window` call,
   then failure on the very next attempt and every one after.** Fixed by enabling management for the
   length of the call and disabling it in a `finally`; verified 5/5 against that same sequence.

   Two things made this cost hours, and both are now fixed. Client lifecycle was **unlogged**, so the
   receiving server's record was a timeout with nothing before it; and a reclaim timeout did not name
   the clients it *did* hold. The line that broke it open was
   `clients held: … FjXD88ok…(disconnected)`.

   **Music Assistant also steals our players' sockets, but only while streaming.**
   `.7.226` (MA 2.9.11) accounted for **16 of the last 20 handshakes** on `.7.122`'s player, arriving
   every ~40-60 s, and each one makes the player log `server dialed us` → `detached from server`. A
   client holds exactly ONE websocket, so every MA dial evicts whichever Plum server currently holds
   that player. This is what makes cross-server roam intermittent on VLAN 7: the same route that
   paired in 170 ms at 15:21:57 returned `ok:false` at 15:26, with MA dialling in between. It is not
   a pairing bug and not a regression — MA is a third-party server legitimately claiming a speaker it
   has configured. **Test cross-server roam with the Plum speakers removed from MA, or MA stopped**,
   and treat any roam measurement taken on VLAN 7 with MA running as contaminated. The real fix is
   the arbitration policy hook this item already notes we do not have. **Stopping the MA stream is
   enough — it stopped dialling within minutes and had been quiet for 12-16 min before the
   directional failure above was measured, so MA does not explain that one. Removing the speakers
   from MA is not required.**
   **Also found: "Open the mesh for pairing" skips any unit whose player has roamed away.** The
   `management` activity is a property of a live connection between a server and a client, so a unit
   whose own player is currently attached to a PEER's source has no connection to open a window on:
   `POST /api/mesh/pairing-window` returns `ok:false`, and returns `ok:true` the moment the player is
   routed home. Verified both ways on `.7.122`. The GUI degrades visibly ("N of M opened") but names
   no reason, and "route your speakers home before adding a unit" is not a rule anyone would guess.
   **MA 2.10 interop, measured end to end 2026-08-13.** Two gates, one now fixed:
   1. ~~We squatted on our own player, so MA's dial was admitted and dropped~~ — **FIXED** by
      releasing an idle player (`b1c1fe0`, `0fbf2a1`). Proven: `Plum Amp100` flipped to
      `available=True` in MA at 19:27:28, and MA now holds our player's socket continuously.
   2. **Still open: MA connects but activates nothing.** MA registers both units as protocol
      players and keeps the connection up, yet reports `available=False` — because it holds no
      pairing record with our player and `pairing.unpairedAccess` is off, so it activates no roles.
      Our side's last activation is by our own server; nothing since MA attached. MA's log shows it
      never *attempted* a pairing. Resolution is a posture choice: pair MA to the player (MA 2.10
      does ship pairing code — `providers/sendspin/security.py`, PIN-eviction tasks — so it likely
      exposes the action), or set `pairing.unpairedAccess` on, which is the sentinel path the spec
      calls MITM-vulnerable. Note MA's AirPlay bridge (`204 AP`) works throughout and is unaffected.
   **What MA can and cannot show about a speaker another server is driving** — settled by reading
   `providers/sendspin/provider.py` @ 2.10.0b15, not inferred from behaviour:
   - It cannot show **what** we are playing on a shared speaker, and never will without a spec
     change. A client holds exactly ONE websocket, so while our server holds it MA has no connection
     through which to learn anything at all.
   - It does not currently show that it **lost** the device either. `ClientDisconnectedEvent` is a
     no-op for player state — MA's own comment reads *"Transport lifecycle events, implemented in
     another PR."* It drops a player only on `ClientRemovedEvent` (the library's ~180 s registry
     cleanup, or an unreachable listener). So a clean handover leaves the player sitting in MA as
     `available=True, state=idle`. A beta gap on their side, and their comment says it is coming.
   - **Consequence, and it IS ours:** pressing play there takes the speaker back mid-stream, because
     arbitration admits an incoming connection of EQUAL rank. Whether a unit should refuse a foreign
     claim while playing a local source is an open design decision with a real cost to the interop
     enabled on 2026-08-13/14 — see the arbitration gap at the head of this item.

   **MA's AirPlay sender emits no `prgr` and no ssnc state codes.** Measured over 19 minutes and 7
   track changes: metadata and artwork on every track, and not one `prgr`/`pbeg`/`prsm`/`paus`. Play
   state therefore rides shairport's MPRIS `PlaybackStatus` (`airplay_remote` →
   `AirplayMetadataReader.note_external_state`). **Progress is genuinely unavailable for that sender**
   — with no `prgr` there is no position or duration in existence, and inventing one would be worse
   than showing none. MA also wraps our two endpoints as ONE `universal_player` with a switchable
   "active output protocol" (Sendspin or AirPlay), which is why the same box appears twice.
   **Side effect of releasing idle players: a peer's speaker now looks adoptable.** An unattached
   Plum player advertises over mDNS like any other client, so it appears in `/api/mesh/neighbourhood`
   as a "foreign" speaker and can be adopted rather than reclaimed. Harmless in itself — both paths
   land the same player on the same source — but `t4_adopt_release.sh` silently started testing a
   sibling unit instead of an ESP32, and then FAILED its lingering-socket assertion on behaviour that
   is correct for a Plum peer (the peer's own server re-dials its player once we let go). The suite
   now excludes every URL the mesh knows as a `local_player`. Worth remembering before reading any
   neighbourhood-driven result: `is_own` no longer means "not one of ours".
7. **amd64 has never been built.**
8. **The APIs are unauthenticated with blanket CORS** (`CORS(app)`, `Access-Control-Allow-Origin: *`,
   both bound to `0.0.0.0`). The injection chain behind it is closed at three layers, but any page on
   the LAN can still change a unit's settings. Deliberately deferred 2026-08-05: restricting CORS
   needs a rig test, because peers and the GUI both call peer `:5001` cross-origin.
9. ~~`_primary_source` is set but never cleared~~ — **fixed 2026-08-06**. Confirmed real by reading:
   `stop_source` popped `sources` and left the id behind, so `_maybe_group_controller` resolved a
   dead source and returned early — a controller with no `ctrl:<source>:` hint silently stopped
   being grouped. It now hands the fallback to the oldest surviving source (`None` when the last one
   goes). Regression-guarded in `test_sendspin_server.py`. Never reproduced on hardware, but the
   read is unambiguous.
10. **A volume change emits two identical `client/state` frames ~2ms apart.** Harmless (it is a full
    report, not a delta) but it means `_publish_render_state` runs twice per command. Seen on the rig
    2026-08-05, not chased.
11. ~~`_is_audio_source` returns True for `A2DP_SINK_UUID`~~ — **resolved 2026-08-06: the comment was
    right, the code was wrong.** 110d (AudioSink) is what a *speaker* advertises; a device offering
    only it cannot send us audio, so adopting it started an `arecord` that could never produce a
    sample and — most-recently-connected wins — took the capture slot from a phone that was already
    playing. The both-match test came in with the original Bluetooth commit (`0ff2ceb`), carried
    from Plum-Snapcast. Now requires 110a; a device that advertises both (phones that can also be a
    speaker) is unaffected, and a skipped sink-only device is logged rather than dropped silently.
12. **Three duplications worth real lines**, from the 2026-08-05 audit: `integrationsService.ts`
    (944 → ~250 with the helper that already exists in `audioService.ts`), `IntegrationsTab.tsx`
    (**1490** as of 2026-08-06, not the ~880 first recorded → ~350 with one endpoint-CRUD card), and
    the three `*_config.py` (431 → ~190 on a shared base). None touch the audio path. Also a shared
    progress/metadata helper for the three source handlers — the Spotify timestamp bug was the third
    implementation of the same plumbing getting it wrong, which is the argument for it.
13. **A follower stops following when its leader switches source.** Found 2026-08-06 while building
    headless mode, and **pre-existing** — it is not about playerless units, it happens identically to
    a leader with a speaker (verified directly). When the leader moves to a second source, the
    follower's old source goes quiet, so its `current_target` becomes `None`; the override guard in
    `follow.tick()` reads that as "the user moved us" and sets `_overridden`, so it never follows to
    the new source. Distinguishing "went idle because the source stopped" from "was deliberately
    moved" needs a real decision, so it was pinned by a parity test
    (`test_a_playerless_leader_switching_source_behaves_like_any_other_leader`) rather than
    quietly changed under a feature branch. `docs/ROUTING-MODEL.md`'s true-none rule 1 landed
    2026-08-12 and speculated this ambiguity might dissolve under it — not re-examined yet; still
    open.
14. **A playerless leader cannot nominate which source it leads with.** With several concurrent
    active sources, `follow._leader_status` picks the one with the most endpoints attached,
    tie-broken by `source_id`. Deterministic and self-reinforcing — the first follower to join raises
    that source's count — and it has to be, because every follower computes it independently with no
    coordination. But the leader has no say, and there is no GUI for it.
15. **A playerless unit's main card has NO endpoint slider** (`hideEndpointVolume`, 2026-08-06).
    It previously rendered a phantom 100% whose `onChange` found no client, did nothing, and snapped
    back on the next poll. Hiding it is rule-conformant — *"the main card's slider is this unit's own
    endpoint, not the group"* — and the group control still exists one panel down in `SyncedDevices`.
    Repurposing that slider to group volume on playerless units would be more useful, but it needs
    that rule **amended explicitly**, not silently excepted. Awaiting a call.

16. **Card-identity hardening — what is still open** (audit 2026-08-06; the confirmed-dangerous ones
    are fixed, see HARD-WON-LESSONS). Ranked:
    - ~~A failed output switch is never retried~~ — **fixed 2026-08-06.** `watch_output_device` now
      holds its baseline until `on_change` reports success (False or a raise = retry), so a card
      that is merely late is picked up on the next tick instead of stranding the unit until a human
      toggles the setting. Logging throttles after the first few attempts. Returning None still
      counts as success.
    - ~~`renderer.device` records the REQUEST, not the card actually opened~~ — **fixed 2026-08-06.**
      `AlsaRenderer.open_device` carries the RESOLVED `<card_name>:<device>` and is what is echoed to
      `player_state.json`; `device` still holds the requested spec, so `reopen`'s no-op check is
      unchanged. `pending` can now detect "opened, but on a different card than intended" — exactly
      what a stale `hw:C,D` produces after a renumber. None when resolution found nothing and
      PortAudio name-matched the raw spec: unknown beats invented.
    - **`_open`'s raw-spec fallback can open the wrong card.** When `aplay -l` fails, resolution
      returns nothing and the raw spec goes to PortAudio, whose names embed `(hw:C,D)` — so an
      `hw:2,0` substring-matches whatever is at that address now and opens it, with one warning.
    - **USB card names are enumeration-order-derived.** Two identical DACs give `Device` and
      `Device_1`, and which is which is decided by the same probe race that moves card numbers, so
      `card_name` is NOT stable for exactly the device class where hot-plugging is normal. Passes
      1–3 of `find_device` have no ambiguity guard at all (only the substring pass does).
    - **`_portaudio_outputs` is last-write-wins** on a duplicate `(card, device)` key, and its 2 s
      cache is keyed on that volatile pair — a hotplug inside the window can hand back an index for
      a device that no longer exists. `resolve_portaudio_index` forces a refresh; no `audio_api`
      caller does.
    - **`parse_aplay_output` silently drops any line the regex misses** — the device then vanishes
      everywhere downstream with nothing logged.

17. **Spotify Connect's first transfer after a go-librespot (re)start can fail.** Seen on
    `.2.10` 2026-08-06 — the first attempt drops immediately, the retry works. It is go-librespot
    internal, NOT our pipeline: `/data/go-librespot/<n>/go-librespot.log` shows
    `failed handling dealer request ... failed creating stream ... failed seeking stream: failed
    reading page: EOF`, i.e. it could not fetch the track from Spotify's CDN. The observed instance
    was ~30 s after a container restart. That log also carries a `panic: send on closed channel`
    from an earlier date — a real go-librespot crash, which our source manager respawns. Worth
    watching for a pattern away from restarts before treating it as ours.

18. **The visualizer's periodic drop-to-zero is CONTROLLER-WS CHURN, not the audio path.** Measured
    in the running GUI on `.2.10` (2026-08-06) by hooking `WebSocket` and timestamping every
    binary frame: spectrum arrives at **31 Hz with a 2000 ms gap every 3 s**, like clockwork
    (1.1 s, 4.1 s, 7.1 s, 10.1 s …). In the same window, **48 controller sockets were created AND
    closed in 22 s** — six (one per source across both units) every ~3 s, all close code 1000.
    Ruled OUT: `refresh_stream`. The server re-acquired the stream only 3 times in the whole log,
    each right after a container restart, so steady playback is not churning the group. The player
    logs no xruns and no starvation.
    ~~The amplifier is `sendspinControllerClient.open()`~~ — **fixed 2026-08-06.** `reconnectAttempts`
    was reset in `onopen`, the instant the socket opened rather than once it had proven stable, so a
    socket dying shortly after connecting reset the counter every cycle and retried at a flat 1 s
    forever. Now forgiven only after `RECONNECT_STABLE_MS` (10 s) of survival. This removes the
    AMPLIFIER, not the cause — a real trigger will now present as a visibly SLOWING retry rather
    than a fixed 3-second sawtooth, which is more diagnosable, not less.
    **STATUS 2026-08-06: not reproducing on `5801dfe`.** Michael reports it looks good with that
    build deployed, after the localActivity/slave ping-pong fix (#13) and the layout fixes landed.
    That is an observation from watching, NOT a measurement, and the sawtooth above WAS measured on
    the same build — so treat this as "intermittent / trigger-dependent", not "fixed". If it returns,
    start from the WebSocket hook rather than from theory; the recipe is in this entry.
    **The TRIGGER — what closes the socket ~1 s after open — is NOT yet identified.** One strong
    candidate not yet excluded: the measurement tab was backgrounded (confirmed — a 100 ms sampler
    was throttled to ~1 Hz), and `client/time` is sent on an adaptive `setTimeout` (0.2–3 s) which
    background throttling would stretch, possibly past whatever the server tolerates. Re-measure in
    a FOCUSED, foreground tab before concluding this is user-visible rather than an artifact.

19. ~~Two greenfield units advertise the same AirPlay receiver name~~ — **fixed 2026-08-06**, on
    Michael's call. Found deploying the alpha to the re-imaged mesh-pair units: `DEFAULT_SETTINGS`'
    endpoints all defaulted to `deviceName: "Plum Audio"`, and AirPlay's is enabled on RAOP 5050 out
    of the box, so every fresh unit offered an identical receiver — "Plum Audio" twice on the LAN with
    nothing to tell them apart. Same defect as the unit name (`2f9c1d9`) one level down. All three
    source endpoints now derive from `PLUM_UNIT_NAME` via `DEFAULT_ENDPOINT_NAME`; Spotify and
    Bluetooth were collision-bound the same way once enabled. Both env-derived defaults are
    `sanitize_device_name`'d **at import**, because `_sanitize_device_names` runs on the write path
    only — a default otherwise reaches disk, and the config renderers, unscrubbed.
20. ~~An unhinted third-party Sendspin controller was silently grouped into `_primary_source`
    whether or not it was actually playing~~ — **fixed 2026-08-12.** Only our own GUI ever sends
    the `"ctrl:<source_id>:"` naming hint `_maybe_group_controller` uses to pick a source; anything
    else (Music Assistant, any conformant third-party Sendspin controller) fell straight through to
    `_primary_source` regardless of state, so a controller connecting while everything was idle
    could land in a `playback_state=stopped` group with nothing in the protocol to mark it as dead —
    "picking up a stream that isn't live." `_default_controller_source` now prefers the primary
    source only while `feeder.is_active`, else the first active source, else leaves the client
    ungrouped. An explicit hint still always wins, idle or not — this only changes the ambiguous
    default. Does not touch player routing or the audio path.
    Related but explicitly out of scope: `docs/ROUTING-MODEL.md` rule 1 ("true none") is a separate,
    larger, already-staged proposal about *players* staying attached to a dead source and
    auto-resuming — not implemented, not part of this fix.

19. **Volume calibration and loudness matching are NOT hardware-validated.** The whole slice
   (curve model, tone, matcher, GUI) is implemented and unit-tested — 131 backend tests, 17 frontend
   — but nothing has been on the rig. `docs/VOLUME-CALIBRATION.md` carries a numbered rig checklist;
   item 2 is the one that matters most, because it is the predecessor's fatal defect: **confirm the
   endpoint's own volume actually changes the tone's measured SPL.** If 35% and 85% read the same,
   the tone is not passing through the gain stage and nothing downstream can be trusted. Known
   soft spots, all unproven either way:
   - The FIFO writer opens non-blocking and polls for the feeder's read end (5 s deadline). Fine in
     tests; untested against a real `SourceFeeder` under load.
   - The matcher's ~2 s settle after a slider release may feel laggy on a real drag. The GUI's
     5 s optimistic `VOLUME_HOLD_MS` should mask it, but that pairing has not been watched.
   - Pink-noise synthesis is ~0.1 s on this workstation; a Pi will be several times slower. It runs
     in an executor, but the first Play may still feel sluggish.
   - `sets` scope has a GUI editor but no rig test.
   - Tone-then-restore has never raced a real roam or a follow tick.
20. **`docs/CLAUDE.md` is 354 lines against its own ~280-line budget.** It was already 333
   before the calibration rules landed. The three new bullets each meet the file's own bar ("an agent
   would break something without it"), so the fix is to move OTHER material out — the maintenance
   note itself prescribes OPEN-ITEMS / HARD-WON-LESSONS / PHASE-HISTORY as the destinations — not to
   drop these. Not attempted here because deciding what stops being a rule is a judgement call about
   material this change did not touch.

21. ~~**`reclaim_remote_player` can stage a pairing PSK against a CLEARTEXT third-party speaker.**~~ — **FIXED 2026-08-21**, option (a) as sketched below. Staging now requires POSITIVE evidence the id is one of ours: it is some unit's own speaker per `MeshView.unit_by_own_player` (conclusive — our own players are never cleartext, and it survives a peer predating the `security` field), or the holding unit reported a non-None `security`. `security is None` is deliberately NOT read as evidence of cleartext, because the field defaults to None and an older peer is indistinguishable from a genuine cleartext client — so the ambiguous case skips staging, loudly logged, rather than knocking a speaker offline. `Router._may_stage_pairing` decides and passes `stage_pairing=` down the engine seam; the server no longer stages on its own authority. Six tests in `test_mesh_routing.py`. **Still wants a rig test on the `.7` pair**: confirm a normal Plum-to-Plum roam still pairs and does not land silent. Original report below.

    
    Pre-existing; nothing to do with calibration, but found while scoping it.
    `sendspin_server.py:1278` calls `stage_shared_psk(player_id)` justified by the comment at
    `:1276-1277`: *"`player_id` came from a peer snapshot, so this only ever names a Plum player."*
    **The premise is false.** `snapshot()` (`:1547-1553`) filters on anchor prefix, connectedness and
    player role family — there is **no ownership test** — so an adopted foreign speaker sits in a
    peer's `players` list exactly like a Plum player. `stage_shared_psk` (`:846-865`) checks only for
    an existing record or existing staging; it has no cleartext guard. CLAUDE.md and
    `docs/SENDSPIN-PAIRING.md` both state that a pairing handshake against a cleartext client is
    aborted by the library and takes that speaker offline (signature: the NEXT adopt succeeds).
    Reachable today by cross-routing an adopted ESP32 between two units.
    Not fixed here because the obvious guard is not actually available: staging happens deliberately
    BEFORE the dial, so there is no connected client whose `connection_security` could be read. Real
    options, none free — (a) carry a `security`/cleartext flag through the peer snapshot and check it
    (`PlayerState.security` is already published and `None` means cleartext, so this may be cheap);
    (b) have the reclaim consult `speaker_names`/adoption records to tell an adopted foreign id from
    a Plum peer id; (c) restrict the reclaim path to ids that resolve to a unit's `local_player`.
    Option (a) looks right and is worth a rig test on the `.7` pair.
