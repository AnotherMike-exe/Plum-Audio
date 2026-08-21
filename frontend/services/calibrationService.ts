import type {
  CalibrationSnapshot,
  EndpointCalibration,
  LoudnessMatchPolicy,
  ToneState,
} from '../types';

/**
 * Loudness calibration — the API client for both halves of the feature.
 *
 * There are two backends and the split is not incidental. CURVES are configuration and live on the
 * Flask config API (:5002, proxied at /api/audio), which is persistence-only and cannot make a
 * sound. The TONE lives on the mesh API (:5001, /api/mesh), because playing it means creating a
 * source and routing a player, and only the audio event loop can do that.
 *
 * READ merged, WRITE local. A record is written to the unit SERVING this page — a peer's :5002 is
 * deliberately not reachable cross-origin — but the endpoint it describes may be grouped on another
 * unit entirely. So reads come from the mesh's merged view, which unions every unit's records and
 * resolves duplicates newest-first. Without that, which unit's page you happened to open would
 * silently decide what the tab showed.
 */

const CONFIG_BASE = '/api/audio/calibration';
const MESH_BASE = import.meta.env.VITE_MESH_API_URL || '/api/mesh';

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.error || body.message || `HTTP ${response.status}`);
  }
  return body as T;
}

function jsonBody(payload: unknown): RequestInit {
  return {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  };
}

/** The fields a record actually owns. Everything else on EndpointCalibration is derived. */
export interface CalibrationDraft {
  name: string;
  url?: string | null;
  enabled: boolean;
  samples: Array<{volume: number; db: number}>;
  maxLimit: {mode: 'percentage' | 'decibel'; value: number};
  trimDb: number;
}

/** A render endpoint that can be calibrated. */
export interface CalibrationEndpoint {
  /**
   * Mesh player id. For an idle third-party speaker this is PROVISIONALLY its listener URL — the
   * only thing mDNS gives us — and the real id (its handshake id, generally a MAC) comes back from
   * the tone start, which adopts it. Save the record under that, never under the URL: a URL is
   * IP-derived and moves with DHCP.
   */
  playerId: string;
  name: string;
  url: string | null;
  unitId: string;
  unitName: string;
  volume: number;
  connected: boolean;
  /** Not one of our players: a third-party Sendspin speaker. See `foreignNotes` in the section UI. */
  foreign: boolean;
  /** True when it is only visible over mDNS — nothing holds it, so toning it must adopt it first. */
  idle: boolean;
}

interface MeshViewPayload {
  units?: Array<{
    unit_id: string;
    name: string;
    host?: string | null;
    players?: Array<{
      player_id: string;
      name?: string;
      url?: string | null;
      volume?: number;
      connected?: boolean;
    }>;
    local_player?: {player_id?: string; name?: string; url?: string | null; volume?: number} | null;
  }>;
}

interface NeighbourhoodPayload {
  players?: Array<{
    name?: string;
    friendly_name?: string;
    url?: string;
    host?: string;
    is_own?: boolean;
  }>;
}

// Server-side bookkeeping clients, not speakers: `src:` anchors a source's group, `ctrl:` is a
// GUI controller websocket. Neither renders audio and neither can be calibrated.
function isInternalClient(clientId: string): boolean {
  return clientId.startsWith('src:') || clientId.startsWith('ctrl:');
}

export const calibrationService = {
  /** This unit's stored records plus the match policy and the wizard's bounds. */
  async getSnapshot(): Promise<CalibrationSnapshot> {
    return request<CalibrationSnapshot>(CONFIG_BASE);
  },

  /** Every unit's records, merged newest-wins. What the tab should render. */
  async getMerged(): Promise<Record<string, EndpointCalibration>> {
    return request<Record<string, EndpointCalibration>>(`${MESH_BASE}/calibration`);
  },

  /**
   * Persist one endpoint's record.
   *
   * Rejected with a 400 when the measurements cannot describe a speaker — loudness must rise with
   * volume. That is not pedantry: a flat response means the tone was not coming from the endpoint
   * being measured, and it is the exact failure the predecessor stored silently and then divided by.
   */
  async save(playerId: string, draft: CalibrationDraft): Promise<EndpointCalibration> {
    return request<EndpointCalibration>(`${CONFIG_BASE}/${encodeURIComponent(playerId)}`, jsonBody(draft));
  },

  async remove(playerId: string): Promise<CalibrationSnapshot> {
    return request<CalibrationSnapshot>(`${CONFIG_BASE}/${encodeURIComponent(playerId)}`, {method: 'DELETE'});
  },

  async setPolicy(policy: LoudnessMatchPolicy): Promise<CalibrationSnapshot> {
    return request<CalibrationSnapshot>(`${CONFIG_BASE}/policy`, jsonBody(policy));
  },

  // -- the tone -------------------------------------------------------------

  async toneStatus(): Promise<ToneState> {
    return request<ToneState>(`${MESH_BASE}/calibration/tone`);
  },

  /**
   * Play the tone from ONE endpoint at `volume`.
   *
   * This ROUTES the endpoint onto a transient calibration source, so it stops playing whatever it
   * was on; the backend remembers where it was and puts it back on stop. It also expires on its
   * own, because a browser that navigates away cannot press Stop.
   */
  async toneStart(
    playerId: string,
    volume: number,
    opts?: {type?: 'pink' | 'sine'; seconds?: number; freq?: number; url?: string | null},
  ): Promise<ToneState> {
    return request<ToneState>(`${MESH_BASE}/calibration/tone`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        player_id: playerId,
        volume,
        // An idle third-party speaker cannot be routed — nothing holds it and it is in no unit's
        // player list. Passing its listener URL lets the backend adopt it instead, and the reply's
        // `playerId` is then the id its handshake gave, which the record must be keyed on.
        url: opts?.url ?? undefined,
        type: opts?.type ?? 'pink',
        seconds: opts?.seconds,
        freq: opts?.freq,
      }),
    });
  },

  /** Re-level a running tone without restarting it, so the noise does not gap between steps. */
  async toneVolume(volume: number): Promise<ToneState> {
    return request<ToneState>(`${MESH_BASE}/calibration/tone/volume`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({volume}),
    });
  },

  async toneStop(): Promise<ToneState> {
    return request<ToneState>(`${MESH_BASE}/calibration/tone/stop`, {method: 'POST'});
  },

  // -- endpoints ------------------------------------------------------------

  /**
   * Every addressable render endpoint in the mesh, for the calibration list.
   *
   * Read straight from the mesh view rather than threaded down from MeshApp, so this section stays
   * self-contained like OutputDeviceSection. Anchor clients (`src:`) and GUI controllers (`ctrl:`)
   * are server-side bookkeeping, not speakers, and are filtered out.
   *
   * A unit's own speaker is folded in from its `local_player` self-report as well as its `players`
   * list: while it is claimed by another server — a peer, or Music Assistant — it appears in no
   * unit's `players` at all, and would otherwise vanish from the list mid-calibration.
   */
  async getEndpoints(): Promise<CalibrationEndpoint[]> {
    // The neighbourhood is a SEPARATE surface from the mesh view: the view covers Plum units, which
    // answer /api/mesh/snapshot, while the neighbourhood is everything else mDNS can see. An idle
    // third-party speaker is in no unit's players and no unit's local_player, so without this it is
    // invisible to calibration — and it is precisely the case the feature needs to reach.
    const [view, neighbourhood] = await Promise.all([
      request<MeshViewPayload>(`${MESH_BASE}/view`),
      request<NeighbourhoodPayload>(`${MESH_BASE}/neighbourhood`).catch(() => ({}) as NeighbourhoodPayload),
    ]);

    const byId = new Map<string, CalibrationEndpoint>();
    const ownPlayerIds = new Set<string>();
    const knownUrls = new Set<string>();
    const unitHosts = new Set<string>();

    for (const unit of view.units ?? []) {
      if (unit.host) unitHosts.add(unit.host);
      const own = unit.local_player;
      if (own?.player_id) ownPlayerIds.add(own.player_id);
    }

    for (const unit of view.units ?? []) {
      for (const player of unit.players ?? []) {
        if (isInternalClient(player.player_id)) continue;
        if (player.url) knownUrls.add(player.url);
        byId.set(player.player_id, {
          playerId: player.player_id,
          name: player.name || player.player_id,
          url: player.url ?? null,
          unitId: unit.unit_id,
          unitName: unit.name,
          volume: player.volume ?? 100,
          connected: player.connected !== false,
          // Attached, but not any unit's OWN speaker — so it is a third-party device, and its
          // reported volume is its connect-time value rather than a live echo.
          foreign: !ownPlayerIds.has(player.player_id),
          idle: false,
        });
      }

      const own = unit.local_player;
      if (own?.player_id && !isInternalClient(own.player_id) && !byId.has(own.player_id)) {
        if (own.url) knownUrls.add(own.url);
        byId.set(own.player_id, {
          playerId: own.player_id,
          name: own.name || unit.name,
          url: own.url ?? null,
          unitId: unit.unit_id,
          unitName: unit.name,
          volume: own.volume ?? 100,
          connected: true,
          foreign: false,
          idle: false,
        });
      }
    }

    for (const entry of neighbourhood.players ?? []) {
      if (!entry.url || entry.is_own) continue;
      if (knownUrls.has(entry.url)) continue; // already listed above, attached, under its real id
      if (entry.host && unitHosts.has(entry.host)) continue; // one of our own units' players
      byId.set(entry.url, {
        // Provisional. Adoption reports the handshake id, and that is what the record keys on.
        playerId: entry.url,
        name: entry.friendly_name || entry.name || entry.url,
        url: entry.url,
        unitId: '',
        unitName: 'Not on a Plum unit',
        volume: 100,
        connected: false,
        foreign: true,
        idle: true,
      });
    }

    return [...byId.values()].sort((a, b) => a.name.localeCompare(b.name));
  },

  // -- presentation helpers -------------------------------------------------

  /**
   * Is this endpoint pinned at its ceiling right now?
   *
   * Computed here rather than reported by the matcher: the GUI already has the endpoint's resolved
   * ceiling and its current level, so asking the backend would add a poll to learn something it can
   * derive. The tolerance absorbs the rounding between a commanded percentage and the player's echo.
   */
  isAtLimit(cal: EndpointCalibration | undefined, volume: number): boolean {
    if (!cal?.calibrated) return false;
    return volume >= Math.min(100, cal.effectiveMaxVolume) - 1;
  },

  /** "Range: 48–79 dB · Max 85% · −3 dB trim" — the one-line summary under an endpoint's name. */
  summarize(cal: EndpointCalibration | undefined): string {
    if (!cal) return 'Not calibrated';
    if (cal.fitRejected) return 'Measurements rejected — re-measure';
    const parts: string[] = [];
    if (cal.dbRange) {
      parts.push(`Range ${Math.round(cal.dbRange.lowDb)}–${Math.round(cal.dbRange.highDb)} dB`);
    }
    parts.push(
      cal.maxLimit.mode === 'decibel'
        ? `Max ${Math.round(cal.maxLimit.value)} dB`
        : `Max ${Math.round(cal.maxLimit.value)}%`,
    );
    if (cal.trimDb) parts.push(`${cal.trimDb > 0 ? '+' : ''}${cal.trimDb} dB trim`);
    if (!cal.enabled) parts.push('matching off');
    return parts.join(' · ');
  },
};
