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

