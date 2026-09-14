/**
 * Unit update service — reads and drives the host update agent, per unit.
 *
 * The unit whose page you opened is reached same-origin (nginx proxies /api/mesh to :5001). Every
 * OTHER unit is reached at `http://<host>:5001` directly, which is the pattern the GUI already uses
 * for a peer's volume and pairing. It has to be :5001 and not the config API: a peer's :5002 is
 * deliberately unreachable cross-origin, and this page must drive a SET of units.
 *
 * There is no server-side fan-out on purpose. One unit reporting on another's update would have to
 * describe a failure ("peer 3 never came back") that this page can observe directly and attribute
 * to the right row. So the page addresses each unit itself.
 *
 * THE RESTART WINDOW is the thing this file exists to get right. Applying an update kills the very
 * container that is answering these requests, so for roughly ten seconds every call to that unit
 * fails at the network layer. That is SUCCESS in progress, not an error, and `pollUntilSettled`
 * below is what keeps a row reading "restarting" instead of flashing a failure at the operator.
 */

const MESH_API_PORT = 5001;
const MESH_API_BASE = import.meta.env.VITE_MESH_API_URL || '/api/mesh';

/** How long a unit may be unreachable after we asked it to update, before we call it lost. A pull
 *  plus a recreate measured ~13 s on a Pi; this is generous so a slow link is not a false alarm. */
const RESTART_GRACE_MS = 180_000;

export type UpdatePhase = 'idle' | 'pulling' | 'restarting' | 'failed' | string;

export interface UpdateAgent {
  installed: boolean;
  version: string | null;
  lastSeen: string | null;
}

export interface UpdateResult {
  result: 'ok' | 'failed' | string;
  message: string;
  at: string;
  fromDigest: string | null;
  toDigest: string | null;
}

export interface UnitUpdateStatus {
  running: { version: string; buildType: string; gitDescribe: string | null };
  agent: UpdateAgent;
  channels: string[];
  pending: boolean;
  requestedAt: number | null;
  stale: boolean;
  channel?: string | null;
  image?: string | null;
  /** The digest this unit is RUNNING. Null on a unit whose image came from a tarball — it has no
   *  registry digest at all, which is a real state and not an error. */
  digest?: string | null;
  /** The digest the registry holds for the channel. Null means we could not ask (no internet on an
   *  isolated AV VLAN is normal), which must never be rendered as "up to date". */
  available?: string | null;
  lastCheck?: string | null;
  lastUpdate?: UpdateResult | null;
  phase?: UpdatePhase;
}

/**
 * The outcome of asking one unit for its status.
 *
 * `no-endpoint` is separate from `unreachable` because they need different actions and look
 * identical from a bare fetch. A unit running an image from before this feature answers 405 for
 * GET /api/mesh/update while serving audio and its GUI perfectly. Collapsing that into
 * "unreachable" sends the operator to check the network, when the fix is to deploy a newer image.
 * Measured on .7.200/.203/.204, which all answered 405 with a healthy /api/mesh/snapshot.
 */
export type StatusProbe =
  | { kind: 'ok'; status: UnitUpdateStatus }
  | { kind: 'no-endpoint' }
  | { kind: 'unreachable' };

/** What a row shows. Derived rather than stored, so it cannot drift from the fields above. */
export type UnitUpdateState =
  | 'unreachable'
  | 'needs-image'
  | 'no-agent'
  | 'unknown'
  | 'up-to-date'
  | 'update-available'
  | 'pulling'
  | 'restarting'
  | 'failed';

export function deriveState(probe: StatusProbe): UnitUpdateState {
  if (probe.kind === 'unreachable') return 'unreachable';
  if (probe.kind === 'no-endpoint') return 'needs-image';
  const status = probe.status;
  if (!status.agent.installed) return 'no-agent';
  if (status.phase === 'pulling') return 'pulling';
  if (status.phase === 'restarting') return 'restarting';
  if (status.phase === 'failed') return 'failed';
  // A pending request that the agent has not picked up yet still reads as work in progress — but a
  // STALE one means the agent is installed and not running, which is a failure the operator can act
  // on and must not look like a slow pull forever.
  if (status.pending) return status.stale ? 'failed' : 'pulling';
  if (!status.available || !status.digest) return 'unknown';
  return status.available === status.digest ? 'up-to-date' : 'update-available';
}

/** Short form of a sha256 digest, for a table cell. Returns '—' for absent. */
export function shortDigest(digest: string | null | undefined): string {
  if (!digest) return '—';
  const hex = digest.startsWith('sha256:') ? digest.slice(7) : digest;
  return hex.slice(0, 12);
}

class UpdateService {
  /** unit_id -> host, filled by the caller from the mesh view it already polls. */
  private hosts = new Map<string, string>();
  private localUnitId: string | null = null;

  setUnits(units: Array<{ unit_id: string; host: string }>, localUnitId: string | null): void {
    this.hosts = new Map(units.map((u) => [u.unit_id, u.host]));
    this.localUnitId = localUnitId;
  }

  /** Where to read the mesh view from. Same-origin in production (nginx proxies /api/mesh); a full
   *  URL during `npm run dev`, where vite proxies the config APIs but not this one. Going through
   *  here rather than hard-coding '/api/mesh/view' is what makes the tab drivable against a real
   *  unit from a workstation. */
  viewUrl(): string {
    return `${MESH_API_BASE}/view`;
  }

  private base(unitId: string): string {
    // The unit serving this page goes same-origin through nginx. Addressing it by IP would work but
    // would also break a dev run against VITE_MESH_API_URL, and would drop the proxy's same-origin
    // handling for no gain.
    if (unitId === this.localUnitId) return MESH_API_BASE;
    const host = this.hosts.get(unitId);
    return host ? `http://${host}:${MESH_API_PORT}/api/mesh` : MESH_API_BASE;
  }

  /** Ask one unit for its status. Never throws — an unreachable unit is an expected answer here,
   *  because applying an update kills the container that would otherwise reply. */
  async status(unitId: string): Promise<StatusProbe> {
    let res: Response;
    try {
      res = await fetch(`${this.base(unitId)}/update`);
    } catch {
      // A network-layer failure. Either the unit is genuinely down, or it is mid-restart, or the
      // browser refused the cross-origin call. The caller decides which, from context.
      return { kind: 'unreachable' };
    }
    // The unit ANSWERED and does not have this route: an image from before the feature existed.
    // aiohttp says 405 rather than 404 because the OPTIONS catch-all matches /api/mesh/{tail} on
    // path but not on method, so both have to count.
    if (res.status === 404 || res.status === 405) return { kind: 'no-endpoint' };
    if (!res.ok) return { kind: 'unreachable' };
    try {
      return { kind: 'ok', status: (await res.json()) as UnitUpdateStatus };
    } catch {
      return { kind: 'unreachable' };
    }
  }

  async statusAll(unitIds: string[]): Promise<Map<string, StatusProbe>> {
    const entries = await Promise.all(
      unitIds.map(async (id) => [id, await this.status(id)] as const),
    );
    return new Map(entries);
  }

  /**
   * Ask one unit to act. Returns the unit's own message on refusal, which is the useful half — a
   * unit with no agent answers 409 naming provision.sh, and showing that beats "failed".
   */
  async apply(
    unitId: string,
    channel: string,
    opts: { checkOnly?: boolean; token?: string } = {},
  ): Promise<{ ok: boolean; message?: string }> {
    try {
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      if (opts.token) headers['X-Plum-Update-Token'] = opts.token;
      const res = await fetch(`${this.base(unitId)}/update`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ channel, check_only: !!opts.checkOnly }),
      });
      if (res.status === 404 || res.status === 405) {
        // Naming the fix matters: "HTTP 405" sends the reader to the network, and the answer is a
        // deploy. This is every unit still on an image from before the update feature.
        return {
          ok: false,
          message: 'this unit runs an image without the update endpoint — deploy a newer image first',
        };
      }
      const body = await res.json().catch(() => ({}));
      if (!res.ok) return { ok: false, message: body?.error || `HTTP ${res.status}` };
      return { ok: true };
    } catch (err) {
      return { ok: false, message: err instanceof Error ? err.message : 'unreachable' };
    }
  }

  /**
   * Poll one unit until its update settles, calling back on every change.
   *
   * The unit disappears mid-way through — that is what an update IS — so an unreachable reply is
   * reported as 'restarting' rather than as a failure, for up to RESTART_GRACE_MS. Only past that
   * does silence become 'unreachable'. Without this the operator sees a red row at the exact moment
   * everything is working.
   */
  async pollUntilSettled(
    unitId: string,
    onState: (state: UnitUpdateState, status: UnitUpdateStatus | null) => void,
    opts: { intervalMs?: number; graceMs?: number; signal?: AbortSignal } = {},
  ): Promise<UnitUpdateState> {
    const interval = opts.intervalMs ?? 2000;
    const grace = opts.graceMs ?? RESTART_GRACE_MS;
    const startedAt = Date.now();
    // The unit must be seen to COME BACK before we trust an 'up-to-date' reading. Without this a
    // poll landing in the ~1 s between the request write and the agent picking it up would read the
    // pre-update state and declare success immediately.
    let sawItGo = false;

    for (;;) {
      if (opts.signal?.aborted) return 'unknown';
      const probe = await this.status(unitId);
      const elapsed = Date.now() - startedAt;

      if (probe.kind === 'unreachable') {
        sawItGo = true;
        if (elapsed > grace) {
          onState('unreachable', null);
          return 'unreachable';
        }
        onState('restarting', null);
      } else if (probe.kind === 'no-endpoint') {
        // The unit answered without the route. Nothing is coming, so stop rather than waiting out
        // the whole restart grace on an image that can never satisfy this request.
        onState('needs-image', null);
        return 'needs-image';
      } else {
        const status = probe.status;
        const state = deriveState(probe);
        onState(state, status);
        if (state === 'failed') return state;
        // Settled means: back up, nothing pending, and either we watched it go away and return, or
        // the agent has already recorded a result for this run.
        const busy = state === 'pulling' || state === 'restarting';
        if (!busy && (sawItGo || status.lastUpdate)) return state;
        if (!busy && elapsed > grace) return state;
      }
      await new Promise((resolve) => setTimeout(resolve, interval));
    }
  }
}

export const updateService = new UpdateService();
