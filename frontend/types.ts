export interface Track {
    id: string;
    title: string;
    artist: string;
    album: string;
    albumArtUrl: string;
    duration: number; // in seconds
}

// Volume Calibration Types
export interface CalibrationReference {
    volume: number;      // Hardware volume % where measurement was taken (e.g., 38 or 80)
    measuredDb: number;  // User's dB reading at that volume
}

export interface CalibrationMaxLimit {
    mode: 'percentage' | 'decibel';
    value: number;  // Either hardware % (0-100) or dB level
}

export interface EndpointCalibration {
    name: string;                    // Friendly name for the endpoint
    calibrated: boolean;             // Whether calibration has been completed
    lowRef?: CalibrationReference;   // Measurement at ~38% volume
    highRef?: CalibrationReference;  // Measurement at ~80% volume
    maxLimit: CalibrationMaxLimit;   // Maximum volume limit
    defaultVolume: number;           // Default startup volume (0-100%)
    lastCalibrated?: string;         // ISO timestamp of last calibration
}

export interface AudioCalibrationSettings {
    [clientId: string]: EndpointCalibration;
}

// Federation: Server representation
export interface Server {
    id: string;           // "server-192-168-7-122"
    name: string;         // "Main Server"
    host: string;         // "198.51.100.20"
    port: number;         // 1780
    connected: boolean;
    isLocal: boolean;
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
    volume?: number;                 // Source volume (0-100) from control script
    // A sender is actively using this source. Idle sources stay routable but drop out of the
    // stream list, so the picker shows what is in use rather than every configured endpoint.
    active?: boolean;
}

export interface Client {
    id: string;                      // Federated ID: "server-192-168-7-122-living-room"
    serverId?: string;               // "server-192-168-7-122" (for federation)
    serverName?: string;             // "Main Server" (for federation)
    name: string;
    currentStreamId: string | null;
    volume: number; // 0-100
    connected: boolean;
    isLocal?: boolean;               // this unit's OWN player (the page you are looking at)
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
            enabled: boolean;
            deviceName: string;
            adapter: string;
            autoPair: boolean;
            discoverable: boolean;
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
            masterHost: string;
            masterWsPort: number;
            masterStreamPort: number;
        };
    };
    snapclientTarget?: string; // Runtime: where snapclient is currently connected (host:port)
    audio?: {
        output?: {
            device: string;
            device_type: string;
            fallback_device: string;
        };
        input?: {
            devices: Array<{
                hw_id: string;
                custom_name: string;
                enabled: boolean;
            }>;
        };
        calibration?: AudioCalibrationSettings;
    };
}
