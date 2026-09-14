export interface Track {
    id: string;
    title: string;
    artist: string;
    album: string;
    albumArtUrl: string;
    duration: number; // in seconds
}

// Volume Calibration Types
//
// An endpoint's loudness is measured, not assumed: the tone plays from one endpoint at a few known
// volumes, the user reads SPL from the listening position, and the backend fits
// `dB = a*log10(volume) + b`. Everything derived from that fit (`curve`, `calibrated`,
// `effectiveMaxVolume`, `dbRange`) is computed server-side and sent down — the GUI never refits,
// so there is exactly one implementation of the maths. See backend/scripts/calibration.py.

export interface CalibrationSample {
    volume: number;  // endpoint volume percent the tone played at, 1-100
    db: number;      // SPL the user read from the listening position
}

export interface CalibrationMaxLimit {
    mode: 'percentage' | 'decibel';
    value: number; // hardware % (0-100), or an absolute SPL resolved through this endpoint's curve
}

export interface CalibrationCurve {
    a: number;        // dB per decade of volume; ~20 for a pure amplitude scaler
    b: number;        // dB at volume 1
    n: number;        // samples the fit was built from
    rmsError: number; // RMS residual in dB; 0 for a two-point (exact) fit
    suspect: boolean; // the points do not lie on a line well enough to trust the extrapolation
}

export interface CalibrationDbRange {
    // Never starts at volume 0 — that is silence, and reporting a finite dB for it is a lie.
    lowVolume: number;
    lowDb: number;
    highVolume: number;
    highDb: number;
}

export interface EndpointCalibration {
    playerId?: string;               // mesh player id (the X25519 peer id) this record is keyed by
    name: string;                    // denormalised display copy; a speaker's name depends on where
    url?: string | null;             // denormalised display copy; moves with DHCP, never a key
    enabled: boolean;                // participate in loudness matching
    samples: CalibrationSample[];
    maxLimit: CalibrationMaxLimit;
    trimDb: number;                  // persistent per-room taste, applied on top of every match
    lastCalibrated?: string | null;  // ISO timestamp; DISPLAY ONLY — see `rev`
    /**
     * Causal version, not a clock. A Pi has no RTC, so ordering two copies of one endpoint's record
     * by timestamp rests entirely on NTP — and gets it silently wrong when a clock is unsynced or
     * has jumped. A save stores max(highest rev seen anywhere, the local rev) + 1 instead.
     */
    rev?: number;
    // --- derived server-side; never written back ---
    calibrated: boolean;
    fitRejected: boolean;            // measurements exist but cannot describe a speaker
    curve: CalibrationCurve | null;
    effectiveMaxVolume: number;
    dbRange: CalibrationDbRange | null;
}

// How far loudness matching reaches. A SEPARATE question from calibration: a curve says how loud
// one endpoint is, this says which endpoints are locked to each other.
export type LoudnessMatchMode = 'off' | 'stream' | 'follow' | 'sets';

export interface LoudnessMatchSet {
    id: string;
    name: string;
    members: string[]; // mesh player ids
}

export interface LoudnessMatchPolicy {
    mode: LoudnessMatchMode;
    sets: LoudnessMatchSet[];
}

export interface CalibrationSnapshot {
    calibrations: Record<string, EndpointCalibration>;
    policy: LoudnessMatchPolicy;
    modes: LoudnessMatchMode[];
    suggestedVolumes: number[];
    minSamples: number;
    maxSamples: number;
}

export interface ToneState {
    playing: boolean;
    playerId?: string;
    sourceId?: string;
    volume?: number;
    toneType?: 'pink' | 'sine';
    elapsed?: number;
    remaining?: number;
}

// Federation: Server representation
export interface Server {
    id: string;           // "server-192-168-7-122"
    name: string;         // "Main Server"
    host: string;         // "198.51.100.20"
    port: number;         // 1780
    connected: boolean;
    isLocal: boolean;
    /**
     * Does this unit have an audio output at all? False for an ingest/routing-only node — no player
     * process, no speaker, and nothing to route audio ONTO. Not the same as "no players connected
     * right now", which is also briefly true at every boot.
     */
    hasPlayer: boolean;
}

// Playback position data from backend (server-side interpolation)
export interface PlaybackData {
    position: number;              // Last known position in milliseconds
    duration: number;              // Total duration in milliseconds
    interpolated_position: number; // Server-interpolated position in milliseconds
    playback_status: 'playing' | 'paused' | 'stopped' | 'unknown';
    is_stale: boolean;             // True if >30s without backend update
}

export interface Stream {
    id: string;                      // Federated ID: "server-192-168-7-122-airplay1"
    serverId?: string;               // "server-192-168-7-122" (for federation)
    serverName?: string;             // "Main Server" (for federation)
    name: string;
    sourceDevice: string;
    currentTrack: Track;
    isPlaying: boolean;
    progress: number;                // in seconds (legacy, frontend-tracked)
    playback?: PlaybackData;         // Server-provided playback position (preferred)
    // GROUP volume (0-100): the average level of the endpoints rendering this source, maintained by
    // the Sendspin controller role. Setting it redistributes across them, preserving relative levels.
    volume?: number;
    muted?: boolean;                 // group mute (true only when every endpoint is muted)
    // SOURCE volume (0-100): the level on the SENDING device — the phone's AirPlay/Bluetooth slider,
    // the Spotify Connect device volume. Stacks with the endpoint levels above and is the only one
    // visible on the sender's own screen. Outside the Sendspin spec; carried on our mesh snapshot.
    sourceVolume?: number;
    sourceMuted?: boolean;
    // Whether this source can report/accept one right now (no live sender → no source volume). The
    // second slider is gated on this rather than shown dead, as with repeat/shuffle.
    supportsSourceVolume?: boolean;
    // A sender is actively using this source. Idle sources stay routable but drop out of the
    // stream list, so the picker shows what is in use rather than every configured endpoint.
    active?: boolean;
    // Controller commands this source advertises (Sendspin controller role supported_commands). The
    // transport UI shows only controls whose command is present here — so repeat/shuffle appear for
    // sources that support them (Spotify) and are hidden for those that don't (AirPlay).
    supportedCommands?: string[];
    repeat?: 'off' | 'one' | 'all'; // current group repeat mode (undefined = not reported)
    shuffle?: boolean;              // current group shuffle state
}

export interface Client {
    id: string;                      // Federated ID: "server-192-168-7-122-living-room"
    serverId?: string;               // "server-192-168-7-122" (for federation)
    serverName?: string;             // "Main Server" (for federation)
    name: string;
    currentStreamId: string | null;
    volume: number; // 0-100 — this endpoint's own output level, as the player reports it
    muted?: boolean;
    connected: boolean;
    isLocal?: boolean;               // this unit's OWN player (the page you are looking at)
    // Set when this speaker has been claimed by a Sendspin server outside our mesh — Music
    // Assistant, a third-party server, anything. It is still ours physically, it is simply
    // rendering someone else's audio, and the GUI must say so rather than lose the device.
    foreignServer?: { name: string; title?: string; artist?: string };
    // A Sendspin speaker on the network that is not one of ours — found by mDNS, not by a unit
    // snapshot. Routing it uses adopt/release (dial its URL) rather than the mesh router.
    isForeign?: boolean;
    url?: string;
    /** Whether this device needs Sendspin pairing before it can play.
     *
     *  `undefined`/'unknown' means we have not been told — a peer on an older image, or a speaker
     *  we have never connected to — and must render NO pair affordance, the same defaulting rule
     *  as `hasPlayer`. 'cleartext' devices (ESP32 speakers, Music Assistant, this GUI) can never
     *  pair. Only 'unpaired' both needs pairing and can be paired. See pairingStateOf. */
    pairingState?: 'paired' | 'trusted' | 'unpaired' | 'cleartext' | 'unknown';
}

export type AccentColor = 'purple' | 'blue' | 'green' | 'orange' | 'red' | 'yellow' | 'custom';
export type ThemeMode = 'light' | 'dark' | 'system' | 'black' | 'white';

// Visualizer types
export type VisualizerType = 'bars' | 'circular' | 'circular-bars' | 'waveform' | 'mixed';
export type VisualizerTheme = 'smart' | 'user' | 'random';
export type VisualizerSmoothingType = 'catmull-rom' | 'bezier' | 'simple';
export type VisualizerIdleState = 'circle' | 'pulse' | 'nothing';
export type VisualizerFrequencyScale = 'linear' | 'logarithmic' | 'logarithmic-smooth';
export type VisualizerRotationDirection = 'clockwise' | 'counterclockwise';

export interface VisualizerSettings {
    enabled: boolean;
    theme: VisualizerTheme;           // Color theme (smart=album art, user=accent, random=cycling)
    type: VisualizerType;              // Waveform type
    barCount: 32 | 64 | 128 | 256;    // Number of frequency bars
    sensitivity: number;               // 0-100, default 50
    smoothing: number;                 // 0-100, default 70 (FFT smoothing)
    smoothingType: VisualizerSmoothingType; // Spline interpolation method
    frequencyScale: VisualizerFrequencyScale; // Frequency distribution calculation
    idleState: VisualizerIdleState;   // What to show when no audio
    symmetry: 1 | 2 | 3 | 4;          // Symmetry multiplier: repeat pattern N times around circle
    mirror: boolean;                   // Mirror pattern (highs center, lows edges)
    invert: boolean;                   // Invert mirror effect (lows center, highs edges)
    taper: boolean;                    // Fade bars/spectrum at edges (bars/wave only)
    mixedFlip: boolean;                // For 'mixed' type: false=bars top/wave bottom, true=wave top/bars bottom
    rotate: boolean;                   // Enable rotation for circular visualizers
    rotationSpeed: number;             // Rotation speed (0-100, default 30)
    rotationDirection: VisualizerRotationDirection; // Rotation direction
    cycleEnabled: boolean;             // Enable cycling through presets on track change
    cyclePresetIds: string[];          // IDs of presets to cycle through
    advanced: {
        bassAnalysis: boolean;         // Enable bass/treble separation
        bassColor?: string;
        midsColor?: string;
        trebleColor?: string;
        particles: boolean;            // Enable particle effects
        particleCount?: number;
        particleLife?: number;
    };
}

export interface VisualizerPreset {
    id: string;
    name: string;
    isBuiltIn?: boolean;               // Built-in presets cannot be edited or deleted
    settings: Omit<VisualizerSettings, 'enabled' | 'cycleEnabled' | 'cyclePresetIds'>; // All settings except enabled and cycle settings
}

export const DEFAULT_VISUALIZER_SETTINGS: VisualizerSettings = {
    enabled: true,  // Always enabled
    theme: 'user',  // Always follow GUI theme
    type: 'circular',
    barCount: 128,
    sensitivity: 50,
    smoothing: 70,
    smoothingType: 'catmull-rom',
    frequencyScale: 'logarithmic-smooth',
    idleState: 'circle',
    symmetry: 1,
    mirror: false,
    invert: false,
    taper: true,
    mixedFlip: false,
    rotate: false,
    rotationSpeed: 30,
    rotationDirection: 'clockwise',
    cycleEnabled: false,
    cyclePresetIds: [],
    advanced: {
        bassAnalysis: false,
        particles: false,
    }
};

// Built-in visualizer presets (cannot be edited or deleted)
export const BUILT_IN_PRESETS: VisualizerPreset[] = [
    {
        id: 'builtin-spectrum-bars',
        name: 'Spectrum Bars',
        isBuiltIn: true,
        settings: {
            theme: 'user',
            type: 'bars',
            barCount: 64,
            sensitivity: 15,
            smoothing: 90,
            smoothingType: 'catmull-rom',
            frequencyScale: 'linear',
            idleState: 'pulse',
            symmetry: 1,
            mirror: true,
            invert: false,
            taper: true,
            mixedFlip: false,
            rotate: false,
            rotationSpeed: 30,
            rotationDirection: 'clockwise',
            advanced: {
                bassAnalysis: false,
                particles: false,
            }
        }
    },
    {
        id: 'builtin-spectrum-wave',
        name: 'Spectrum Wave',
        isBuiltIn: true,
        settings: {
            theme: 'user',
            type: 'waveform',
            barCount: 32,
            sensitivity: 15,
            smoothing: 80,
            smoothingType: 'simple',
            frequencyScale: 'logarithmic-smooth',
            idleState: 'pulse',
            symmetry: 1,
            mirror: true,
            invert: false,
            taper: true,
            mixedFlip: false,
            rotate: false,
            rotationSpeed: 30,
            rotationDirection: 'clockwise',
            advanced: {
                bassAnalysis: false,
                particles: false,
            }
        }
    },
    {
        id: 'builtin-tri-circular',
        name: 'Tri Circular',
        isBuiltIn: true,
        settings: {
            theme: 'user',
            type: 'circular',
            barCount: 32,
            sensitivity: 15,
            smoothing: 85,
            smoothingType: 'catmull-rom',
            frequencyScale: 'linear',
            idleState: 'pulse',
            symmetry: 3,
            mirror: true,
            invert: false,
            taper: true,
            mixedFlip: false,
            rotate: true,
            rotationSpeed: 1,
            rotationDirection: 'clockwise',
            advanced: {
                bassAnalysis: false,
                particles: false,
            }
        }
    },
    {
        id: 'builtin-bi-circular',
        name: 'Bi Circular',
        isBuiltIn: true,
        settings: {
            theme: 'user',
            type: 'circular',
            barCount: 32,
            sensitivity: 15,
            smoothing: 85,
            smoothingType: 'catmull-rom',
            frequencyScale: 'linear',
            idleState: 'pulse',
            symmetry: 2,
            mirror: true,
            invert: false,
            taper: true,
            mixedFlip: false,
            rotate: true,
            rotationSpeed: 1,
            rotationDirection: 'clockwise',
            advanced: {
                bassAnalysis: false,
                particles: false,
            }
        }
    },
    {
        id: 'builtin-tri-radial',
        name: 'Tri Radial',
        isBuiltIn: true,
        settings: {
            theme: 'user',
            type: 'circular-bars',
            barCount: 128,
            sensitivity: 15,
            smoothing: 85,
            smoothingType: 'catmull-rom',
            frequencyScale: 'linear',
            idleState: 'pulse',
            symmetry: 3,
            mirror: true,
            invert: true,
            taper: true,
            mixedFlip: false,
            rotate: true,
            rotationSpeed: 1,
            rotationDirection: 'clockwise',
            advanced: {
                bassAnalysis: false,
                particles: false,
            }
        }
    },
    {
        id: 'builtin-bi-radial',
        name: 'Bi Radial',
        isBuiltIn: true,
        settings: {
            theme: 'user',
            type: 'circular-bars',
            barCount: 128,
            sensitivity: 15,
            smoothing: 85,
            smoothingType: 'catmull-rom',
            frequencyScale: 'linear',
            idleState: 'pulse',
            symmetry: 2,
            mirror: true,
            invert: true,
            taper: true,
            mixedFlip: false,
            rotate: true,
            rotationSpeed: 1,
            rotationDirection: 'clockwise',
            advanced: {
                bassAnalysis: false,
                particles: false,
            }
        }
    },
    {
        id: 'builtin-radial',
        name: 'Radial',
        isBuiltIn: true,
        settings: {
            theme: 'user',
            type: 'circular-bars',
            barCount: 128,
            sensitivity: 15,
            smoothing: 85,
            smoothingType: 'catmull-rom',
            frequencyScale: 'linear',
            idleState: 'pulse',
            symmetry: 1,
            mirror: true,
            invert: true,
            taper: true,
            mixedFlip: false,
            rotate: true,
            rotationSpeed: 1,
            rotationDirection: 'clockwise',
            advanced: {
                bassAnalysis: false,
                particles: false,
            }
        }
    },
    {
        id: 'builtin-circular',
        name: 'Circular',
        isBuiltIn: true,
        settings: {
            theme: 'user',
            type: 'circular',
            barCount: 32,
            sensitivity: 15,
            smoothing: 85,
            smoothingType: 'catmull-rom',
            frequencyScale: 'linear',
            idleState: 'pulse',
            symmetry: 1,
            mirror: true,
            invert: false,
            taper: true,
            mixedFlip: false,
            rotate: true,
            rotationSpeed: 1,
            rotationDirection: 'clockwise',
            advanced: {
                bassAnalysis: false,
                particles: false,
            }
        }
    },
    {
        id: 'builtin-spectrum-mixed',
        name: 'Spectrum Mixed',
        isBuiltIn: true,
        settings: {
            theme: 'user',
            type: 'mixed',
            barCount: 64,
            sensitivity: 15,
            smoothing: 80,
            smoothingType: 'simple',
            frequencyScale: 'logarithmic-smooth',
            idleState: 'pulse',
            symmetry: 1,
            mirror: true,
            invert: false,
            taper: true,
            mixedFlip: false,
            rotate: false,
            rotationSpeed: 30,
            rotationDirection: 'clockwise',
            advanced: {
                bassAnalysis: false,
                particles: false,
            }
        }
    }
];

// AirPlay Endpoint
export interface AirPlayEndpoint {
    id: string;
    enabled: boolean;
    deviceName: string;
    port: number;
    udpPortBase: number;
}

// Spotify Endpoint
export interface SpotifyEndpoint {
    id: string;
    enabled: boolean;
    deviceName: string;
    zeroconfPort: number;
}

// Bluetooth Endpoint — one per ADAPTER, so realistically one per unit. Modelled as an array
// anyway so the source manager, endpoint CRUD and GUI card are shared with AirPlay/Spotify.
export interface BluetoothEndpoint {
    id: string;
    enabled: boolean;
    deviceName: string;
    adapter: string;
}

// DLNA/UPnP Endpoint
export interface DLNAEndpoint {
    id: string;
    enabled: boolean;
    deviceName: string;
    port: number;
    uuid: string;
}

export interface Settings {
    deviceName: string;
    hostname: string;
    integrations: {
        airplay: {
            endpoints: AirPlayEndpoint[];
        };
        bluetooth: {
            // autoPair/discoverable are section-level: they describe how the integration pairs,
            // not one radio. The endpoints array is keyed by adapter (see BluetoothEndpoint).
            autoPair: boolean;
            discoverable: boolean;
            endpoints: BluetoothEndpoint[];
        };
        spotify: {
            bitrate: 96 | 160 | 320;
            endpoints: SpotifyEndpoint[];
        };
        dlna: {
            endpoints: DLNAEndpoint[];
        };
        plexamp: {
            available: boolean;
            enabled: boolean;
            sourceName: string;
        };
        snapcast: boolean;
        visualizer: boolean | VisualizerSettings; // Support legacy boolean
        visualizerPresets?: VisualizerPreset[];   // Saved visualizer presets
    };
    theme: {
        mode: ThemeMode;
        accent: AccentColor;
        customColor?: string; // Hex string (e.g., "#ff5733")
        useAlbumArtColors?: boolean; // Extract accent color from album artwork
    };
    display: {
        showOfflineDevices: boolean;
    };
    federation: {
        enabled: boolean;
        autoDiscover: boolean;
    };
    autoSwitch?: {
        localActivity: boolean;
        slave: {
            enabled: boolean;
            masterUnitId: string | null; // the mesh unit_id to follow while idle
        };
    };
    snapclientTarget?: string; // Runtime: where snapclient is currently connected (host:port)
    audio?: {
        output?: {
            // Null until the user picks one; "none" means this unit deliberately renders nothing.
            // (`fallback_device` used to be here — _migrate_audio_output deletes it on read.)
            device: string | null;
            device_type: string | null;
        };
        input?: {
            devices: Array<{
                hw_id: string;
                custom_name: string;
                enabled: boolean;
            }>;
        };
        calibration?: Record<string, EndpointCalibration>;
    };
}
