import React, {useCallback, useEffect, useRef, useState} from 'react';
import {Routes, Route, useNavigate} from 'react-router-dom';
import {NowPlaying} from './components/NowPlaying';
import {PlayerControls} from './components/PlayerControls';
import {StreamSelector} from './components/StreamSelector';
import {ClientManager} from './components/ClientManager';
import {SyncedDevices} from './components/SyncedDevices';
import {Settings as SettingsModal} from './components/Settings';
import {Visualizer} from './components/Visualizer';
import {getSnapcastData} from './services/snapcastDataService';
import {snapcastService} from './services/snapcastService';
import {federationService} from './services/federationService';
import {settingsService} from './services/settingsService';
import {getStreamPlayback} from './services/playbackService';
import {calibrationService} from './services/calibrationService';
import type {Client, PlaybackData, Server, Settings, Stream, VisualizerPreset, AudioCalibrationSettings} from './types';
import {DEFAULT_VISUALIZER_SETTINGS, BUILT_IN_PRESETS} from './types';
import {useAudioSync} from './hooks/useAudioSync';
import {useBrowserAudioClient} from './hooks/useBrowserAudioClient';
import { Icon } from './components/Icon';
import {updateFavicon} from './utils/favicon';
import {getTextColorForBackground, lightenColor, darkenColor} from './utils/colorContrast';
import {extractDualColorsFromAlbumArt, type DualColorExtractionResult} from './utils/albumArtColorExtraction';
import musicNotePlaceholderRaw from './src/assets/icons/music-note-placeholder.svg?raw';

// Convert raw SVG to data URI for use in img src
const musicNotePlaceholder = `data:image/svg+xml,${encodeURIComponent(musicNotePlaceholderRaw)}`;

const VOLUME_STEP = 5;

// Helper function to validate album art URLs
// Detects corrupted data URLs containing binary data instead of proper base64
function isValidAlbumArtUrl(url: string | undefined | null): boolean {
    if (!url || typeof url !== 'string' || url.trim() === '') {
        return false;
    }

    // For data URLs, check if they contain binary characters (null bytes, control chars)
    if (url.startsWith('data:image')) {
        // Check for null bytes or other control characters (except newlines)
        // These indicate binary data was incorrectly placed in a string
        for (let i = 0; i < Math.min(url.length, 200); i++) {
            const code = url.charCodeAt(i);
            // Null bytes, or control chars < 32 (except \n=10, \r=13, \t=9)
            if (code === 0 || (code < 32 && code !== 10 && code !== 13 && code !== 9)) {
                console.error(`[ArtValidation] ❌ Corrupted data URL detected at char ${i}: byte=${code} (${url.substring(0, 100)}...)`);
                return false;
            }
        }

        // Valid base64 should only contain A-Z, a-z, 0-9, +, /, =, and whitespace
        const base64Part = url.substring(url.indexOf(',') + 1);
        if (base64Part && !/^[A-Za-z0-9+/=\s]+$/.test(base64Part.substring(0, 100))) {
            console.error(`[ArtValidation] ❌ Invalid base64 characters detected: ${base64Part.substring(0, 100)}...`);
            return false;
        }
    }

    return true;
}

const App: React.FC = () => {
    const [streams, setStreams] = useState<Stream[]>([]);
    const [clients, setClients] = useState<Client[]>([]);
    const [servers, setServers] = useState<Server[]>([]);
    const [serverName, setServerName] = useState<string>('Snapcast Server');
    const [streamCapabilities, setStreamCapabilities] = useState<{canSeek?: boolean}>({});
    const [isLoading, setIsLoading] = useState(true);
    const [connectionError, setConnectionError] = useState<string | null>(null);
    const [preMuteGroupVolumes, setPreMuteGroupVolumes] = useState<Record<string, Record<string, number>>>({});
    const [isSettingsOpen, setIsSettingsOpen] = useState(false);
    const [settingsInitialTab, setSettingsInitialTab] = useState<string | undefined>(undefined);
    const [isVisualizerOpen, setIsVisualizerOpen] = useState(false);

    // Album art dual-color extraction state (background + accent)
    const [extractedAlbumArtColors, setExtractedAlbumArtColors] = useState<DualColorExtractionResult | null>(null);
    const [isExtractingColor, setIsExtractingColor] = useState(false);

    // Volume calibration state
    const [calibrations, setCalibrations] = useState<AudioCalibrationSettings>({});

    // Track recent user-initiated changes to prevent polling from overwriting them
    const [recentPlaybackChange, setRecentPlaybackChange] = useState<{streamId: string, timestamp: number} | null>(null);
    const [recentUserChanges, setRecentUserChanges] = useState<{type: string, timestamp: number, data: any} | null>(null);
    // Use refs to avoid stale closure in WebSocket and polling callbacks
    const recentPlaybackChangeRef = useRef<{streamId: string, timestamp: number} | null>(null);
    const recentUserChangesRef = useRef<{type: string, timestamp: number, data: any} | null>(null);
    // Track volume change timestamps to reject stale polling data
    const volumeChangeTimestamps = useRef<Record<string, {volume: number, timestamp: number}>>({});
    // Use ref to access current streams without circular dependency
    const streamsRef = useRef<Stream[]>(streams);
    // Use ref to prevent concurrent fetchData calls
    const isFetchingRef = useRef(false);
    // Track last seen timestamps for clients to prevent removing them during transient backend issues
    const clientLastSeenRef = useRef<Record<string, number>>({});
    // Grace period for keeping clients that temporarily disappear from snapshots (10 seconds)
    const CLIENT_RETENTION_GRACE_MS = 10000;

    // Settings loaded from settingsService (server + local storage)
    const [settings, setSettings] = useState<Settings>(settingsService.getMergedSettings());

    // Store group mappings for clients
    const [clientGroupMap, setClientGroupMap] = useState<Record<string, string>>({});

    // Federation active endpoint (for multi-server stream routing)
    const [activeEndpoint, setActiveEndpoint] = useState<{ active: boolean; serverId?: string; clientId?: string; streamId?: string }>({ active: false });

    // Update streamsRef when streams changes
    useEffect(() => {
        streamsRef.current = streams;

        // Track volume changes to reject stale polling data
        const now = Date.now();
        streams.forEach(stream => {
            if (stream.volume !== undefined) {
                const lastKnown = volumeChangeTimestamps.current[stream.id];
                if (!lastKnown || lastKnown.volume !== stream.volume) {
                    // Volume changed - update timestamp
                    volumeChangeTimestamps.current[stream.id] = {volume: stream.volume, timestamp: now};
                }
            }
        });
    }, [streams]);

    // Browser audio client for "Listen in Browser" functionality
    const browserAudio = useBrowserAudioClient(window.location.hostname);

    // Track if we've already auto-assigned the browser client to prevent loops
    const [browserClientAutoAssigned, setBrowserClientAutoAssigned] = useState(false);
    // Capture the target stream when "Listen in Browser" is clicked
    const [targetStreamForBrowserAudio, setTargetStreamForBrowserAudio] = useState<string | null>(null);

    // Visualizer preset cycling state
    const [previousTrackId, setPreviousTrackId] = useState<string | null>(null);
    const [currentCycleIndex, setCurrentCycleIndex] = useState(0);

    // Initialize settings from service (fetch from server + local storage)
    useEffect(() => {
        settingsService.init().then((initialSettings) => {
            setSettings(initialSettings);
        }).catch((error) => {
            console.error('[Settings] Failed to initialize:', error);
        });

        // Subscribe to settings changes
        const unsubscribe = settingsService.subscribe((updatedSettings) => {
            setSettings(updatedSettings);
        });

        return () => unsubscribe();
    }, []);

    // Update browser title when device name changes
    useEffect(() => {
        if (settings.deviceName) {
            document.title = settings.deviceName;
        }
    }, [settings.deviceName]);

    // Update favicon when theme changes
    useEffect(() => {
        updateFavicon(settings.theme.accent, settings.theme.customColor, settings.theme.mode);
    }, [settings.theme.accent, settings.theme.customColor, settings.theme.mode]);

    useEffect(() => {
        const root = document.documentElement;

        const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
        const handleSystemThemeChange = (e: MediaQueryListEvent) => {
            if (settings.theme.mode === 'system') {
                root.setAttribute('data-theme', e.matches ? 'dark' : 'light');
            }
        };

        if (settings.theme.mode === 'system') {
            root.setAttribute('data-theme', mediaQuery.matches ? 'dark' : 'light');
            mediaQuery.addEventListener('change', handleSystemThemeChange);
        } else {
            root.setAttribute('data-theme', settings.theme.mode);
            mediaQuery.removeEventListener('change', handleSystemThemeChange);
        }

        root.setAttribute('data-accent', settings.theme.accent);

        // Determine the effective theme mode (resolve 'system' to actual mode)
        const effectiveMode = settings.theme.mode === 'system'
            ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
            : settings.theme.mode;

        // Check if we're in a monochrome mode
        const isMonochromeMode = effectiveMode === 'black' || effectiveMode === 'white';

        // Only set colors if NOT in monochrome mode
        // Monochrome modes define their own colors in CSS
        if (isMonochromeMode) {
            // Clear any previously set inline styles so CSS takes over
            root.style.removeProperty('--accent-text-color');
            root.style.removeProperty('--bg-primary');
            root.style.removeProperty('--bg-secondary');
            root.style.removeProperty('--bg-tertiary');
            root.style.removeProperty('--bg-secondary-hover');
            root.style.removeProperty('--bg-tertiary-hover');
            root.style.removeProperty('--border-color');
            root.style.removeProperty('--text-primary');
            root.style.removeProperty('--text-secondary');
            root.style.removeProperty('--text-muted');
            root.style.removeProperty('--icon-muted');
            root.style.removeProperty('--accent-color');
            root.style.removeProperty('--accent-color-hover');
            root.style.removeProperty('--custom-accent-color');
            root.style.removeProperty('--custom-accent-color-hover');
            root.style.removeProperty('--control-icon-color');
        } else {
            // Priority: Extracted album art colors (if enabled) > User-selected colors
            if (settings.theme.useAlbumArtColors && extractedAlbumArtColors) {
                // Apply extracted dual colors (background + accent)
                const bgColor = extractedAlbumArtColors.backgroundColor;
                const accentColor = extractedAlbumArtColors.accentColor;
                const isDark = extractedAlbumArtColors.isDarkTheme;

                // Apply background colors
                // --bg-primary: Main background
                root.style.setProperty('--bg-primary', bgColor);

                // Calculate secondary background (slightly lighter for dark, slightly lighter for light)
                const bgSecondary = isDark ? lightenColor(bgColor, 5) : lightenColor(bgColor, 3);
                root.style.setProperty('--bg-secondary', bgSecondary);

                // Calculate tertiary background (slightly lighter than secondary)
                const bgTertiary = isDark ? lightenColor(bgColor, 8) : lightenColor(bgColor, 5);
                root.style.setProperty('--bg-tertiary', bgTertiary);

                // Calculate hover states (slightly lighter than their base)
                const bgSecondaryHover = isDark ? lightenColor(bgSecondary, 5) : lightenColor(bgSecondary, 3);
                root.style.setProperty('--bg-secondary-hover', bgSecondaryHover);

                const bgTertiaryHover = isDark ? lightenColor(bgTertiary, 5) : lightenColor(bgTertiary, 3);
                root.style.setProperty('--bg-tertiary-hover', bgTertiaryHover);

                // Calculate border color (lighter than background)
                const borderColor = isDark ? lightenColor(bgColor, 15) : darkenColor(bgColor, 10);
                root.style.setProperty('--border-color', borderColor);

                // Calculate text colors based on theme
                if (isDark) {
                    // Dark theme: light text on dark background
                    root.style.setProperty('--text-primary', '#f0f0f0');
                    root.style.setProperty('--text-secondary', '#b0b0c0');
                    root.style.setProperty('--text-muted', '#808090');
                    root.style.setProperty('--icon-muted', '#707080');
                } else {
                    // Light theme: dark text on light background
                    root.style.setProperty('--text-primary', '#181c32');
                    root.style.setProperty('--text-secondary', '#5e6278');
                    root.style.setProperty('--text-muted', '#7e8299');
                    root.style.setProperty('--icon-muted', '#a1a5b7');
                }

                // Apply accent colors
                const accentHover = lightenColor(accentColor, 15);
                root.style.setProperty('--accent-color', accentColor);
                root.style.setProperty('--accent-color-hover', accentHover);
                root.style.setProperty('--custom-accent-color', accentColor);
                root.style.setProperty('--custom-accent-color-hover', accentHover);

                // Calculate optimal text color for accent
                const textColor = getTextColorForBackground(accentColor);
                root.style.setProperty('--accent-text-color', textColor);

                // Set control icon color to accent (for media control buttons)
                root.style.setProperty('--control-icon-color', accentColor);
            } else {
                // Clear background and text overrides when not using album art colors
                root.style.removeProperty('--bg-primary');
                root.style.removeProperty('--bg-secondary');
                root.style.removeProperty('--bg-tertiary');
                root.style.removeProperty('--bg-secondary-hover');
                root.style.removeProperty('--bg-tertiary-hover');
                root.style.removeProperty('--border-color');
                root.style.removeProperty('--text-primary');
                root.style.removeProperty('--text-secondary');
                root.style.removeProperty('--text-muted');
                root.style.removeProperty('--icon-muted');
                root.style.removeProperty('--control-icon-color');

                // Determine the effective accent color
                // Priority: Custom color > Built-in color
                let effectiveAccentColor: string;

                if (settings.theme.accent === 'custom' && settings.theme.customColor) {
                    effectiveAccentColor = settings.theme.customColor;
                } else {
                    // Use built-in accent color
                    const accentColors: Record<string, string> = {
                        purple: '#aa5cc3',
                        blue: '#3b82f6',
                        green: '#22c55e',
                        orange: '#f97316',
                        red: '#ef4444',
                        yellow: '#eab308',
                    };
                    effectiveAccentColor = accentColors[settings.theme.accent] || accentColors.purple;
                }

                // Apply the effective accent color
                const accentHover = lightenColor(effectiveAccentColor, 15);
                const textColor = getTextColorForBackground(effectiveAccentColor);

                // Set both --accent-color and --custom-accent-color to ensure it applies
                // regardless of the data-accent attribute
                root.style.setProperty('--accent-color', effectiveAccentColor);
                root.style.setProperty('--accent-color-hover', accentHover);
                root.style.setProperty('--custom-accent-color', effectiveAccentColor);
                root.style.setProperty('--custom-accent-color-hover', accentHover);
                root.style.setProperty('--accent-text-color', textColor);
            }
        }

        return () => {
            mediaQuery.removeEventListener('change', handleSystemThemeChange);
        };
    }, [settings.theme, extractedAlbumArtColors]);

    // Helper to get the local server
    const getLocalServer = () => servers.find(s => s.isLocal);

    // Helper to check if a client/stream ID belongs to the local server
    const isLocalId = (id: string): boolean => {
        if (!settings.federation.enabled) return true;
        const localServer = getLocalServer();
        return !localServer || id.startsWith(`${localServer.id}-`);
    };

    // Helper to strip server prefix from federated IDs
    const stripServerPrefix = (id: string): string => {
        const localServer = getLocalServer();
        if (localServer && id.startsWith(`${localServer.id}-`)) {
            return id.replace(`${localServer.id}-`, '');
        }
        return id;
    };

    // Helper to get the effective browser audio client ID
    // In federation mode, clients get "server-{ip}-" prefix based on which server they're connected to
    const getBrowserAudioClientId = (): string => {
        if (!browserAudio.state.clientId) return '';

        if (settings.federation.enabled) {
            // Determine which server the browser is connected to
            const currentHost = browserAudio.state.currentHost;

            if (currentHost) {
                // Find the server by host IP
                const connectedServer = servers.find(s => s.host === currentHost);
                if (connectedServer) {
                    return `${connectedServer.id}-${browserAudio.state.clientId}`;
                }
            }

            // Fallback to local server ID
            const localServer = getLocalServer();
            if (localServer) {
                return `${localServer.id}-${browserAudio.state.clientId}`;
            }
            // Fallback to localhost prefix if no server found yet
            return `server-localhost-${browserAudio.state.clientId}`;
        }

        return browserAudio.state.clientId;
    };

    // Helper to get server for a stream ID
    // Stream IDs are prefixed with server ID: "server-192-168-1-100-airplay-0"
    const getServerForStream = (streamId: string): Server | undefined => {
        if (!settings.federation.enabled || !streamId) return undefined;
        return servers.find(s => streamId.startsWith(`${s.id}-`));
    };

    // Helper to extract host from a federated stream ID
    // Stream ID format: "server-192-168-1-100-streamname" -> "192.168.1.100"
    const extractHostFromStreamId = (streamId: string): string | undefined => {
        if (!streamId || !streamId.startsWith('server-')) return undefined;
        // Extract: server-192-168-1-100-streamname -> 192-168-1-100
        const match = streamId.match(/^server-(\d+-\d+-\d+-\d+)-/);
        if (match) {
            // Convert dashes back to dots: 192-168-1-100 -> 192.168.1.100
            return match[1].replace(/-/g, '.');
        }
        return undefined;
    };

    // Find the primary client to control
    // Prefer MAC address format (integrated snapclient on Raspberry Pi), otherwise use first client
    // Exclude browser audio client and remote snapclients from being selected as primary
    const browserClientId = getBrowserAudioClientId();
    const localServer = servers.find(s => s.isLocal);
    const localServerId = localServer?.id;

    // Helper to extract the raw snapclient ID (strip any server-X-X-X-X- prefix).
    // In slave mode the local snapclient appears on a REMOTE server, so we must
    // strip any server prefix (not just the local one) to expose the MAC for detection.
    const getClientLocalPart = (clientId: string): string => {
        const match = clientId.match(/^server-[\d-]+-(.+)$/);
        if (match) return match[1];
        return clientId;
    };

    // Helper: returns true if a client belongs to the local server (by ID prefix or
    // the localServerId field added by the federation dedup layer for slave clients).
    // In slave mode the client's ID carries the master's serverId, but localServerId
    // is set to the home server so the frontend still identifies it as local.
    const isLocalServerClient = (c: { id: string; [key: string]: unknown }): boolean => {
        if (!localServerId) return false;
        return c.id.startsWith(`${localServerId}-`) || (c as any).localServerId === localServerId;
    };

    // Find the primary hardware client for this GUI.
    // In federation mode, prefer clients that are on (or came from) the local server —
    // this ensures that the secondary (slave) GUI controls its own hardware, not the master's.
    const myClient = clients.find(c => {
        if (c.id === browserClientId) return false;
        if (c.id.includes('-remote-server-')) return false;
        const localPart = getClientLocalPart(c.id);
        if (!/^[0-9a-f]{2}(:[0-9a-f]{2}){5}$/i.test(localPart)) return false;
        // In federation mode, only pick a local-server client in the first pass
        return localServerId ? isLocalServerClient(c) : true;
    }) || clients.find(c => {
        // Fallback: any MAC-format client that isn't infrastructure or a remote-server client.
        // In federation mode still exclude remote-server hardware clients to avoid controlling
        // the wrong device when the local snapclient is temporarily disconnected.
        if (c.id === browserClientId) return false;
        if (c.id.includes('-remote-server-')) return false;
        if (localServerId && !isLocalServerClient(c)) return false;
        const localPart = getClientLocalPart(c.id);
        return /^[0-9a-f]{2}(:[0-9a-f]{2}){5}$/i.test(localPart);
    }) || clients.find(c =>
        c.id !== browserClientId && !c.id.includes('-remote-server-')
    );

    // Determine current stream:
    // In federation mode, each GUI should show its LOCAL client's stream
    // NOT the federation-wide active endpoint (which is used for lockout, not display)
    let currentStreamId: string | undefined;
    if (settings.federation.enabled && localServerId) {
        // Find the LOCAL hardware client (output client on this server).
        // Matches by ID prefix (client is on local server) OR by localServerId field
        // (client physically lives here but is connected to a remote master in slave mode).
        const localHardwareClient = clients.find(c => {
            if (!isLocalServerClient(c)) return false;
            const localPart = getClientLocalPart(c.id);
            return /^[0-9a-f]{2}(:[0-9a-f]{2}){5}$/i.test(localPart);
        });

        currentStreamId = localHardwareClient?.currentStreamId ?? myClient?.currentStreamId;
    } else {
        // Non-federation mode or no local server found: use myClient's stream
        currentStreamId = myClient?.currentStreamId;
    }
    const currentStream = streams.find(s => s.id === currentStreamId);

    // Debug logging
    if (currentStream && currentStream.volume !== undefined) {
        console.log(`[CurrentStream] id='${currentStream.id}', name='${currentStream.name}', volume=${currentStream.volume}`);
    }

    // Treat none-* streams the same as no stream selected (hide controls)
    // In multi-server mode, none stream IDs are like "server-192-168-201-133-none-snapserver"
    const isNoneStream = currentStream?.id?.includes('none-') ?? false;
    const shouldShowControls = currentStream && !isNoneStream;

    // Auto-assign browser audio client to user's current stream when it connects
    useEffect(() => {
        if (!browserAudio.state.isActive) {
            // Reset flags when browser audio is stopped
            setBrowserClientAutoAssigned(false);
            setTargetStreamForBrowserAudio(null);
            return;
        }

        // Check if browser client exists in client list (server has recognized it)
        // Note: Browser audio client might not report as connected immediately, but if it exists, we can assign it
        const browserClient = clients.find(c => c.id === browserClientId);
        if (!browserClient) {
            return; // Server hasn't reported it yet
        }

        // Skip if already auto-assigned
        if (browserClientAutoAssigned) {
            return;
        }

        // Use the captured target stream (from when button was clicked)
        // Fall back to myClient's current stream if target wasn't captured
        const targetStream = targetStreamForBrowserAudio || myClient?.currentStreamId;

        // Always assign to target stream if we have one
        // This handles both new connections and reconnections with stale stream assignments
        if (targetStream && browserClient.currentStreamId !== targetStream) {
            handleStreamChange(browserClientId, targetStream);
        }

        if (targetStream) {
            setBrowserClientAutoAssigned(true);
        }
    }, [browserAudio.state.isActive, browserAudio.state.clientId, clients, myClient, browserClientAutoAssigned, targetStreamForBrowserAudio, streams, servers, settings.federation.enabled]);

    // Stop browser audio client if its assigned stream is removed or set to none-snapserver
    useEffect(() => {
        if (!browserAudio.state.isActive) {
            return; // Browser audio not active, nothing to do
        }

        // Skip if we're still in the auto-assign phase (haven't assigned a stream yet)
        // This prevents a race condition where this effect runs before auto-assign completes
        if (!browserClientAutoAssigned) {
            return;
        }

        // Find the browser audio client in the client list
        const browserClient = clients.find(c => c.id === browserClientId);
        if (!browserClient) {
            return; // Browser client not registered yet, wait for it to appear
        }

        // Stop if no stream is assigned
        if (!browserClient.currentStreamId) {
            browserAudio.stop();
            return;
        }

        // Stop if assigned to a none stream (fallback when source stream is removed)
        if (browserClient.currentStreamId?.includes('none-')) {
            browserAudio.stop();
            return;
        }

        // Check if the assigned stream still exists
        const assignedStream = streams.find(s => s.id === browserClient.currentStreamId);
        if (!assignedStream) {
            // Stream was removed - stop the browser audio client
            browserAudio.stop();
        }
    }, [browserAudio.state.isActive, streams, clients, browserClientId, browserClientAutoAssigned]);

    // Visualizer preset cycling on track change
    useEffect(() => {
        // Get visualizer settings
        const viz = typeof settings.integrations.visualizer === 'object'
            ? settings.integrations.visualizer
            : DEFAULT_VISUALIZER_SETTINGS;

        // Skip if cycling is disabled or no presets selected
        if (!viz.cycleEnabled || !viz.cyclePresetIds || viz.cyclePresetIds.length === 0) {
            return;
        }

        // Get current track ID
        const currentTrackId = currentStream?.currentTrack?.id;

        // Skip if no track or track hasn't changed
        if (!currentTrackId || currentTrackId === previousTrackId) {
            return;
        }

        // Track has changed - cycle to next preset
        setPreviousTrackId(currentTrackId);

        // Get all presets (built-in + user)
        const userPresets = settings.integrations.visualizerPresets || [];
        const allPresets = [...BUILT_IN_PRESETS, ...userPresets];

        // Filter to only selected presets
        const cyclePresets = allPresets.filter(p => viz.cyclePresetIds.includes(p.id));

        if (cyclePresets.length === 0) {
            return;
        }

        // Get next preset
        const nextIndex = (currentCycleIndex + 1) % cyclePresets.length;
        const nextPreset = cyclePresets[nextIndex];
        setCurrentCycleIndex(nextIndex);

        // Apply preset settings
        const newVisualizerSettings = {
            enabled: viz.enabled,
            cycleEnabled: viz.cycleEnabled,
            cyclePresetIds: viz.cyclePresetIds,
            ...nextPreset.settings
        };

        settingsService.updateServerSettings({
            integrations: {
                ...settings.integrations,
                visualizer: newVisualizerSettings
            }
        });
    }, [currentStream, settings, previousTrackId, currentCycleIndex]);

    // Extract dual colors (background + accent) from album art when artwork changes (if enabled)
    useEffect(() => {
        // Reset colors when feature disabled or no stream/artwork
        if (!settings.theme.useAlbumArtColors || !currentStream?.currentTrack.albumArtUrl) {
            setExtractedAlbumArtColors(null);
            return;
        }

        const artworkUrl = currentStream.currentTrack.albumArtUrl;

        // Skip extraction for placeholder artwork
        if (artworkUrl === musicNotePlaceholder || artworkUrl.startsWith('data:image/svg+xml')) {
            setExtractedAlbumArtColors(null);
            return;
        }

        // Debounce: Don't extract if already extracting
        if (isExtractingColor) return;

        // Determine if current theme is dark
        const effectiveMode = settings.theme.mode === 'system'
            ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
            : settings.theme.mode;
        const isDarkTheme = effectiveMode === 'dark' || effectiveMode === 'black';

        // Get fallback accent color
        const accentColors: Record<string, string> = {
            purple: '#aa5cc3',
            blue: '#3b82f6',
            green: '#22c55e',
            orange: '#f97316',
            red: '#ef4444',
            yellow: '#eab308',
        };
        const fallbackAccent = settings.theme.accent === 'custom' && settings.theme.customColor
            ? settings.theme.customColor
            : accentColors[settings.theme.accent] || accentColors.purple;

        setIsExtractingColor(true);

        extractDualColorsFromAlbumArt(artworkUrl, isDarkTheme, fallbackAccent)
            .then((result) => {
                if (result) {
                    setExtractedAlbumArtColors(result);
                } else {
                    console.warn('[AlbumArtColor] Extraction returned null, using fallback');
                    setExtractedAlbumArtColors(null);
                }
                setIsExtractingColor(false);
            })
            .catch((error) => {
                console.warn('[AlbumArtColor] Extraction failed:', error);
                setExtractedAlbumArtColors(null);
                setIsExtractingColor(false);
            });
    }, [
        currentStream?.currentTrack.albumArtUrl,
        currentStream?.id,
        settings.theme.useAlbumArtColors,
        settings.theme.mode,
        settings.theme.accent,
        settings.theme.customColor,
        isExtractingColor
    ]);

    // Update browser client name, volume, and connection status if server has reported it
    // Volume is managed locally (not synced to server), so override with local state
    const allClients = clients.map(c => {
        // If this is our browser audio client, ensure proper naming, local volume, and connection status
        if (browserAudio.state.isActive && c.id === browserClientId) {
            return {
                ...c,
                // Always show as "This Browser" to distinguish from hardware clients
                name: 'This Browser',
                // Override volume with local state (browser audio volume is local only)
                volume: browserAudio.state.volume,
                // Override connection status - if browser audio is active, it's connected
                connected: true
            };
        }
        return c;
    });

    // Add placeholder if browser audio is active but server hasn't reported it yet
    if (browserAudio.state.isActive) {
        const serverHasClient = clients.some(c => c.id === browserClientId);

        if (!serverHasClient) {
            // Server hasn't seen the client yet - add temporary placeholder
            const browserClient: Client = {
                id: browserClientId,
                name: 'This Browser (Connecting...)',
                currentStreamId: null,
                volume: browserAudio.state.volume,
                connected: false
            };
            allClients.push(browserClient);
        }
    }

    // Helper function to detect if a client should be hidden
    // Hide snapweb/browser clients that are auto-created by the server
    const shouldHideClient = (client: Client): boolean => {
        // Hide snapweb/browser clients (auto-created by server), but not our browser audio client
        if (browserAudio.state.isActive && client.id === browserClientId) {
            // Never hide our browser audio client
            return false;
        }

        // Hide other snapweb/browser clients (auto-created by server)
        const browserIndicators = ['snapweb', 'browser'];
        const clientName = client.name.toLowerCase();
        const isSnapwebClient = browserIndicators.some(indicator => clientName.includes(indicator));

        return isSnapwebClient;
    };

    // Filter clients based on settings
    // Always show browser audio client when active, regardless of offline device setting
    const filteredClients = settings.display.showOfflineDevices
        ? allClients
        : allClients.filter(c =>
            c.connected || (c.id === browserClientId && browserAudio.state.isActive)
        );

    // Filter out remote snapclients from user-visible lists (these are infrastructure, not devices)
    // Remote snapclient IDs contain "remote-server-" after the server prefix
    // e.g., "server-192-168-201-133-remote-server-192-168-201-133"
    const userVisibleClients = filteredClients.filter(c =>
        !c.id.includes('-remote-server-')
    );

    // Synced clients: same stream as current stream, excluding myClient itself
    // Clients on none streams are never considered "synced" (they have no stream)
    const currentStreamIsNone = currentStreamId?.includes('none-') ?? false;
    const syncedClients = userVisibleClients.filter(c =>
        c.id !== myClient?.id &&
        !currentStreamIsNone && // If current stream is none, no synced clients
        c.currentStreamId === currentStreamId &&
        !c.currentStreamId?.includes('none-') && // Clients on none streams are never synced
        !shouldHideClient(c)
    );

    // Other clients: all clients EXCEPT myClient and synced clients
    // Include clients on "none" streams (idle) even if current stream is also none
    // This ensures idle devices are visible when viewing from an idle client
    const otherClients = userVisibleClients.filter(c =>
        c.id !== myClient?.id &&
        (c.currentStreamId !== currentStreamId || c.currentStreamId?.includes('none-')) &&
        !shouldHideClient(c)
    );

    const updateStreamProgress = useCallback((streamId: string, newProgress: number) => {
        setStreams(prevStreams =>
            prevStreams.map(s =>
                s.id === streamId
                    ? {...s, progress: Math.min(newProgress, s.currentTrack.duration)}
                    : s
            )
        );
    }, []);

    useAudioSync(currentStream, updateStreamProgress);

    // Clean up recent playback change after grace period expires
    useEffect(() => {
        if (!recentPlaybackChange) return;

        const gracePeriod = 8000;
        const timeRemaining = gracePeriod - (Date.now() - recentPlaybackChange.timestamp);

        if (timeRemaining > 0) {
            const timeout = setTimeout(() => {
                recentPlaybackChangeRef.current = null;
                setRecentPlaybackChange(null);
            }, timeRemaining);

            return () => clearTimeout(timeout);
        } else {
            recentPlaybackChangeRef.current = null;
            setRecentPlaybackChange(null);
        }
    }, [recentPlaybackChange]);

    // Sync refs with state changes
    useEffect(() => {
        recentPlaybackChangeRef.current = recentPlaybackChange;
    }, [recentPlaybackChange]);

    useEffect(() => {
        recentUserChangesRef.current = recentUserChanges;
    }, [recentUserChanges]);

    // Clean up recent user changes after grace period expires
    useEffect(() => {
        if (!recentUserChanges) return;

        const gracePeriod = 7000; // 7 seconds for user changes (longer than 5s polling interval)
        const timeRemaining = gracePeriod - (Date.now() - recentUserChanges.timestamp);

        if (timeRemaining > 0) {
            const timeout = setTimeout(() => {
                setRecentUserChanges(null);
            }, timeRemaining);

            return () => clearTimeout(timeout);
        } else {
            setRecentUserChanges(null);
        }
    }, [recentUserChanges]);

    // Listen for real-time metadata updates from Snapcast
    useEffect(() => {
        if (!snapcastService) return;

        const unsubscribe = snapcastService.onMetadataUpdate((streamId, metadata) => {
            // Update the stream with new metadata
            // IMPORTANT: Metadata updates are instant and indicate the stream is actively playing
            setStreams(prevStreams =>
                prevStreams.map(stream => {
                    // Map local WebSocket stream ID to federated stream ID
                    const federatedStreamId = getFederatedStreamId(streamId);
                    const isMatch = stream.id === federatedStreamId;

                    if (isMatch) {
                        // Detect if this is a new track (title changed)
                        const isNewTrack = metadata.title && metadata.title !== stream.currentTrack.title;

                        // Update track metadata
                        const updatedTrack = {
                            ...stream.currentTrack,
                            title: metadata.title || stream.currentTrack.title,
                            artist: metadata.artist || stream.currentTrack.artist,
                            album: metadata.album || stream.currentTrack.album,
                            // Update duration when it changes (backend sends in seconds, already converted)
                            duration: metadata.duration ? Math.floor(metadata.duration) : stream.currentTrack.duration,
                        };

                        // Handle artwork updates:
                        // - If artwork explicitly provided and valid → use it
                        // - If new track but no artwork yet → clear to default
                        // - Otherwise → keep current artwork (for partial metadata updates)

                        if (metadata.artUrl && metadata.artUrl.trim() !== '') {
                            // Transform artUrl: relative paths need proper routing
                            let resolvedArtUrl = metadata.artUrl;

                            // Check if it's a data URL (embedded image)
                            if (metadata.artUrl.startsWith('data:')) {
                                // Data URL - use directly
                                resolvedArtUrl = metadata.artUrl;
                            } else if (metadata.artUrl.startsWith('/')) {
                                // Relative path from Snapserver
                                // Route through our CORS proxy for /coverart/ paths to enable ColorThief extraction
                                if (metadata.artUrl.startsWith('/coverart/')) {
                                    // Extract filename and use proxy endpoint (runs on federation service port 5001)
                                    const filename = metadata.artUrl.replace('/coverart/', '');
                                    const apiPort = window.location.hostname === 'localhost' ? '5001' : '5001';
                                    resolvedArtUrl = `http://${window.location.hostname}:${apiPort}/api/settings/proxy/coverart/${filename}`;
                                } else {
                                    // Other relative paths - use Snapserver directly
                                    resolvedArtUrl = `${snapcastService.getHttpUrl()}${metadata.artUrl}`;
                                }
                            }
                            // else: absolute URL - use directly

                            // Validate artwork URL before using it
                            if (isValidAlbumArtUrl(resolvedArtUrl)) {
                                updatedTrack.albumArtUrl = resolvedArtUrl;
                            } else {
                                updatedTrack.albumArtUrl = musicNotePlaceholder;
                            }
                        } else if (isNewTrack) {
                            updatedTrack.albumArtUrl = musicNotePlaceholder;
                        }
                        // else: keep stream.currentTrack.albumArtUrl (already in updatedTrack from spread)

                        // Metadata updates should NOT change playback state
                        // The playbackStateUpdate handler is the single source of truth for play/pause state
                        // Metadata can arrive while paused (some sources send metadata independent of playback)
                        // Metadata can arrive after a pause command but before backend confirmation
                        // Let the playbackStateUpdate handler manage isPlaying state exclusively

                        return {
                            ...stream,
                            currentTrack: updatedTrack,
                            // DO NOT change isPlaying - keep current state
                            // Reset progress to 0 when new track starts
                            progress: isNewTrack ? 0 : stream.progress,
                            // Also reset the attached playback data on a new track. Otherwise
                            // useAudioSync keeps reading the PREVIOUS track's interpolated_position
                            // (still climbing) until the playback API posts the new track's reset —
                            // that race is the progress "flap between previous and new". Zeroing it
                            // here keeps the UI at 0 until fresh data arrives.
                            ...(isNewTrack && stream.playback
                                ? { playback: { ...stream.playback, position: 0, interpolated_position: 0 } }
                                : {})
                        };
                    }
                    return stream;
                })
            );
        });

        return () => unsubscribe();
    }, [settings.federation.enabled, servers]);

    // Listen for real-time playback state updates from Snapcast
    useEffect(() => {
        if (!snapcastService) return;

        const unsubscribe = snapcastService.onPlaybackStateUpdate(async (streamId, playbackStatus, properties) => {
            // Handle refresh signal - fetch latest state for all streams
            if (playbackStatus === 'REFRESH') {
                // In federation mode, skip local refresh - let federation polling handle it
                // Calling fetchData() in federation mode would overwrite federation data with local-only data
                if (settings.federation.enabled) {
                    return;
                }

                // Fetch latest state for all streams (non-federation mode only)
                const serverStatus = await snapcastService.getServerStatus();
                if (serverStatus && serverStatus.server && serverStatus.server.streams) {
                    // Get all current stream IDs (without federation prefix)
                    const currentStreamIds = new Set(streamsRef.current.map(s => getLocalStreamId(s.id)));
                    const newStreamIds = new Set(serverStatus.server.streams.map((s: any) => s.id));

                    // Check if streams have been added or removed
                    const streamsAdded = [...newStreamIds].some(id => !currentStreamIds.has(id));
                    const streamsRemoved = [...currentStreamIds].some(id => !newStreamIds.has(id));

                    if (streamsAdded || streamsRemoved) {
                        // If streams were added/removed, refetch everything (only if not already fetching)
                        if (!isFetchingRef.current) {
                            await fetchData();
                        }
                    } else {
                        // Just update playback state for existing streams
                        setStreams(prevStreams =>
                            prevStreams.map(stream => {
                                // Strip federation prefix for server stream lookup
                                const localStreamId = getLocalStreamId(stream.id);

                                const serverStream = serverStatus.server.streams.find((s: any) => s.id === localStreamId);
                                if (serverStream) {
                                    const isPlaying = snapcastService.isStreamPlaying(serverStream);

                                    // Check if there's a recent user-initiated playback change (use ref for immediate access)
                                    const now = Date.now();
                                    const gracePeriod = 8000;
                                    const recentChange = recentPlaybackChangeRef.current;
                                    const hasRecentChange = recentChange &&
                                        recentChange.streamId === stream.id &&
                                        (now - recentChange.timestamp) < gracePeriod;

                                    if (stream.isPlaying !== isPlaying && hasRecentChange) {
                                        return stream; // Keep current state during grace period
                                    }
                                    // Only update volume if explicitly provided in properties
                                    // Using conditional spread to avoid overwriting with undefined
                                    const sourceVolume = serverStream.properties?.volume;
                                    return {
                                        ...stream,
                                        isPlaying: isPlaying,
                                        ...(typeof sourceVolume === 'number' ? { volume: sourceVolume } : {})
                                    };
                                }
                                return stream;
                            })
                        );
                    }
                }
                return;
            }

            // Handle direct playback status update
            const isPlaying = playbackStatus.toLowerCase() === 'playing';

            // DEBUG: Log playback state updates for Bluetooth
            if (streamId.includes('Bluetooth')) {
                console.log(`[DEBUG onPlaybackStateUpdate] streamId=${streamId}, playbackStatus='${playbackStatus}', isPlaying=${isPlaying}`);
            }

            // Check if this is a new stream that doesn't exist yet
            const federatedStreamId = getFederatedStreamId(streamId);
            const streamExists = streamsRef.current.some(stream => stream.id === federatedStreamId);

            if (!streamExists) {
                // This is a NEW stream - need to fetch data to include it
                // IMPORTANT: In federation mode, DO NOT call fetchData() as it uses local WebSocket data
                // which would overwrite the federation data and cause other servers' devices to disappear.
                // Instead, trigger an immediate federation poll which will properly update state.
                if (settings.federation.enabled) {
                    federationService.triggerPoll().catch(() => {
                        // Silent failure - federation polling will retry
                    });
                    return;
                }

                // Non-federation mode: fetch all data (only if not already fetching)
                if (!isFetchingRef.current) {
                    await fetchData();
                }
                return;
            }

            // Update the stream's playing state
            setStreams(prevStreams =>
                prevStreams.map(stream => {
                    const isMatch = stream.id === federatedStreamId;

                    if (isMatch) {
                        // Check if there's a recent user-initiated playback change (use ref for immediate access)
                        const now = Date.now();
                        const gracePeriod = 8000; // 8 seconds grace period
                        const recentChange = recentPlaybackChangeRef.current;
                        const hasRecentChange = recentChange &&
                            recentChange.streamId === stream.id &&
                            (now - recentChange.timestamp) < gracePeriod;

                        // Only update if no recent user change or state matches expectation
                        if (stream.isPlaying !== isPlaying && hasRecentChange) {
                            return stream; // Keep current state during grace period
                        }
                        // Only update volume if explicitly provided in properties
                        // Using conditional spread to avoid overwriting with undefined
                        const sourceVolume = properties?.volume;
                        return {
                            ...stream,
                            isPlaying: isPlaying,
                            ...(typeof sourceVolume === 'number' ? { volume: sourceVolume } : {})
                        };
                    }
                    return stream;
                })
            );
        });

        return () => unsubscribe();
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [settings.federation.enabled, servers]);

    // Listen for real-time position updates from Snapcast (for sources that support it)
    useEffect(() => {
        if (!snapcastService) return;

        const unsubscribe = snapcastService.onPositionUpdate((streamId, position, duration) => {
            // CRITICAL: Skip position updates for Bluetooth streams
            // Bluetooth uses playback API with server-side interpolation, not Stream.OnUpdate position updates
            // Stream.OnUpdate position for Bluetooth would conflict with playback API and cause timeline jumping
            if (streamId.includes('Bluetooth')) {
                return;
            }

            // Position and duration come in SECONDS from backend (already converted by control script)
            const progressInSeconds = Math.floor(position);

            // Map local WebSocket stream ID to federated stream ID
            const federatedStreamId = getFederatedStreamId(streamId);

            // Update stream progress
            updateStreamProgress(federatedStreamId, progressInSeconds);
        });

        return () => unsubscribe();
    }, [updateStreamProgress, settings.federation.enabled, servers]);

    // Fetch stream capabilities when current stream changes
    useEffect(() => {
        if (!currentStream || !snapcastService) {
            setStreamCapabilities({});
            return;
        }

        const fetchCapabilities = async () => {
            try {
                const capabilities = await snapcastService.getStreamCapabilities(currentStream.id);
                setStreamCapabilities({
                    canSeek: capabilities.canSeek || false
                });
            } catch (error) {
                console.error('Failed to fetch stream capabilities:', error);
                setStreamCapabilities({canSeek: false});
            }
        };

        fetchCapabilities();
    }, [currentStream?.id]);

    // Periodically retry fetching artwork when showing placeholder
    // AirPlay artwork can take 1-10 seconds to arrive in the backend cache
    useEffect(() => {
        if (!currentStream) return;
        if (settings.federation.enabled) return; // Federation mode handles artwork via polling

        // Check if current stream has placeholder artwork
        const hasPlaceholder = currentStream.currentTrack.albumArtUrl === musicNotePlaceholder;

        if (!hasPlaceholder) return; // No need to retry if we have real artwork

        let attemptCount = 0;
        const retryInterval = setInterval(async () => {
            attemptCount++;
            try {
                const freshStream = await snapcastService.getStreamStatus(currentStream.id);
                const artUrl = freshStream?.properties?.metadata?.artUrl;

                if (artUrl && artUrl.trim() !== '') {
                    setStreams(prev => prev.map(s =>
                        s.id === currentStream.id
                            ? {...s, currentTrack: {...s.currentTrack, albumArtUrl: artUrl}}
                            : s
                    ));
                    clearInterval(retryInterval);
                }
            } catch (error) {
                // Silently handle errors - artwork retry is best effort
            }
        }, 1000); // Check every 1 second

        // Stop retrying after 15 seconds
        const timeout = setTimeout(() => {
            clearInterval(retryInterval);
        }, 15000);

        return () => {
            clearInterval(retryInterval);
            clearTimeout(timeout);
        };
    }, [currentStream?.id, currentStream?.currentTrack.albumArtUrl, currentStream?.currentTrack.title]);

    // Periodically sync metadata AND playback state with server as fallback
    // This ensures GUI stays in sync even if WebSocket notifications fail
    // NOTE: Disabled in federation mode - federation polling handles this
    useEffect(() => {
        if (!currentStream || !snapcastService) return;
        if (settings.federation.enabled) return; // Federation polling handles this

        const syncStreamState = async () => {
            try {
                // DEBUG: Log that sync is running for Bluetooth
                if (currentStream.id.includes('Bluetooth')) {
                    console.log(`[DEBUG syncStreamState] Running for ${currentStream.id}`);
                }

                // Fetch both Snapcast stream data and our playback API data in parallel
                // Skip playback API for none streams (they don't have position data)
                const [serverStream, playbackData] = await Promise.all([
                    snapcastService.getStreamStatus(currentStream.id),
                    currentStream.id.includes('none-') ? Promise.resolve(null) : getStreamPlayback(currentStream.id)
                ]);

                if (serverStream) {
                    const isPlaying = snapcastService.isStreamPlaying(serverStream);

                    // Prefer playback API for position, fall back to Snapcast properties
                    let positionSeconds = 0;
                    let durationSeconds = 0;

                    // DEBUG: Log playback API data
                    if (currentStream.id.includes('Bluetooth')) {
                        console.log(`[DEBUG] Playback API data:`, playbackData ? {
                            is_stale: playbackData.is_stale,
                            playback_status: playbackData.playback_status,
                            position: playbackData.position,
                            interpolated_position: playbackData.interpolated_position
                        } : 'null');
                        console.log(`[DEBUG] serverStream.status: ${serverStream.status}`);
                        console.log(`[DEBUG] isStreamPlaying result: ${isPlaying}`);
                    }

                    if (playbackData && !playbackData.is_stale) {
                        // Use playback API (includes server-side interpolation)
                        positionSeconds = Math.floor(playbackData.interpolated_position / 1000);
                        durationSeconds = Math.floor(playbackData.duration / 1000);
                    } else {
                        // Fall back to Snapcast properties
                        positionSeconds = serverStream.properties?.position
                            ? Math.floor(serverStream.properties.position)
                            : 0;
                    }

                    // Extract metadata from stream properties (simple field names)
                    let updatedMetadata = null;
                    if (serverStream.properties?.metadata) {
                        const meta = serverStream.properties.metadata;

                        // Handle artwork URL properly (same as snapcastDataService.ts and WebSocket handler)
                        let albumArtUrl = undefined;
                        if (meta.artUrl) {
                            if (meta.artUrl.startsWith('data:')) {
                                // Data URL - use directly
                                albumArtUrl = meta.artUrl;
                            } else if (meta.artUrl.startsWith('/')) {
                                // Relative path from Snapserver
                                // Route through our CORS proxy for /coverart/ paths to enable ColorThief extraction
                                if (meta.artUrl.startsWith('/coverart/')) {
                                    // Extract filename and use proxy endpoint (runs on federation service port 5001)
                                    const filename = meta.artUrl.replace('/coverart/', '');
                                    const apiPort = window.location.hostname === 'localhost' ? '5001' : '5001';
                                    albumArtUrl = `http://${window.location.hostname}:${apiPort}/api/settings/proxy/coverart/${filename}`;
                                } else {
                                    // Other relative paths - use Snapserver directly
                                    albumArtUrl = `${snapcastService.getHttpUrl()}${meta.artUrl}`;
                                }
                            } else {
                                // Absolute URL - use directly
                                albumArtUrl = meta.artUrl;
                            }
                        }

                        updatedMetadata = {
                            title: meta.title || meta.name,
                            artist: Array.isArray(meta.artist) ? meta.artist.join(', ') : meta.artist,
                            album: meta.album,
                            albumArtUrl: albumArtUrl,
                            duration: durationSeconds > 0 ? durationSeconds : (meta.duration ? Math.floor(meta.duration) : undefined)
                        };
                    }

                    // Prefer out-of-band metadata from the playback API when its record is fresh.
                    // The Snapcast properties.metadata above is delivered via partial Properties
                    // pushes that clobber each other (state pushes carry no metadata), which is the
                    // root of the metadata flap. The playback API is a single consistent source.
                    if (playbackData && !playbackData.is_stale &&
                        (playbackData.title || playbackData.artist || playbackData.album)) {
                        updatedMetadata = {
                            title: playbackData.title || updatedMetadata?.title,
                            artist: playbackData.artist || updatedMetadata?.artist,
                            album: playbackData.album || updatedMetadata?.album,
                            albumArtUrl: playbackData.artUrl || updatedMetadata?.albumArtUrl,
                            duration: durationSeconds > 0 ? durationSeconds : updatedMetadata?.duration
                        };
                    }

                    // Update stream with latest state AND metadata
                    setStreams(prevStreams =>
                        prevStreams.map(s => {
                            if (s.id === currentStream.id) {
                                const updatedStream = { ...s };

                                // NEVER update playback state from polling - WebSocket events are the source of truth
                                // Polling reads from Snapcast server state which may be stale/cached
                                // Note: playback state (isPlaying) is not applied here
                                // WebSocket events provide real-time state changes, polling just syncs metadata

                                // Update metadata if we got new data
                                if (updatedMetadata) {
                                    const isNewTrack = updatedMetadata.title && updatedMetadata.title !== s.currentTrack.title;

                                    updatedStream.currentTrack = {
                                        ...s.currentTrack,
                                        title: updatedMetadata.title || s.currentTrack.title,
                                        artist: updatedMetadata.artist || s.currentTrack.artist,
                                        album: updatedMetadata.album || s.currentTrack.album,
                                        duration: updatedMetadata.duration !== undefined ? updatedMetadata.duration : s.currentTrack.duration,
                                    };

                                    // Handle artwork: use provided, default for new track, or keep current
                                    if (updatedMetadata.albumArtUrl && updatedMetadata.albumArtUrl.trim() !== '') {
                                        // Validate artwork URL before using it
                                        if (isValidAlbumArtUrl(updatedMetadata.albumArtUrl)) {
                                            updatedStream.currentTrack.albumArtUrl = updatedMetadata.albumArtUrl;
                                        } else {
                                            updatedStream.currentTrack.albumArtUrl = musicNotePlaceholder;
                                        }
                                    } else if (isNewTrack) {
                                        updatedStream.currentTrack.albumArtUrl = musicNotePlaceholder;
                                    } else {
                                        updatedStream.currentTrack.albumArtUrl = s.currentTrack.albumArtUrl;
                                    }

                                    // Reset progress to 0 when new track starts
                                    if (isNewTrack) {
                                        updatedStream.progress = 0;
                                    }
                                }

                                // Attach playback API data to stream for useAudioSync hook
                                if (playbackData && !playbackData.is_stale) {
                                    updatedStream.playback = {
                                        position: playbackData.position || 0,
                                        duration: playbackData.duration || 0,
                                        interpolated_position: playbackData.interpolated_position || 0,
                                        playback_status: playbackData.playback_status || 'unknown',
                                        is_stale: playbackData.is_stale ?? true
                                    };

                                    // Source volume now arrives out-of-band via the playback API
                                    // (no Snapcast Properties push = no onResync). Apply it as the
                                    // live source-volume value so the AirPlay line volume tracks.
                                    if (typeof playbackData.volume === 'number') {
                                        updatedStream.volume = playbackData.volume;
                                    }

                                    // CRITICAL: Update isPlaying from playback API for streams with choppy audio
                                    // For Bluetooth and other sources, stream.status toggles rapidly (idle/playing)
                                    // but playback API provides stable playback state from the control script
                                    const playbackApiIsPlaying = playbackData.playback_status === 'playing';

                                    // DEBUG: Always log for Bluetooth streams
                                    if (s.id.includes('Bluetooth')) {
                                        console.log(`[DEBUG] isPlaying comparison: current=${updatedStream.isPlaying}, playbackAPI=${playbackApiIsPlaying}, status='${playbackData.playback_status}'`);
                                    }

                                    if (updatedStream.isPlaying !== playbackApiIsPlaying) {
                                        console.log(`[PlaybackAPI] Updating ${s.id} isPlaying: ${updatedStream.isPlaying} → ${playbackApiIsPlaying}`);
                                        updatedStream.isPlaying = playbackApiIsPlaying;
                                    }
                                }

                                // NOTE: Don't directly update progress here - useAudioSync handles
                                // progress updates with smooth local interpolation. App.tsx only
                                // attaches playback data to the stream; useAudioSync reads it and
                                // manages the actual progress value to avoid oscillation between
                                // the two sources fighting over the progress value.

                                return updatedStream;
                            }
                            return s;
                        })
                    );
                }
            } catch (error) {
                // Log errors for Bluetooth streams to debug
                if (currentStream.id.includes('Bluetooth')) {
                    console.error('[DEBUG syncStreamState] Error:', error);
                }
                // Silently handle errors to avoid spam for other streams
            }
        };

        // Immediately sync on stream change, then poll every 2 seconds
        syncStreamState();
        const interval = setInterval(syncStreamState, 2000);

        return () => clearInterval(interval);
    }, [currentStream?.id, settings.federation.enabled]);

    // Shared function to fetch/refetch all data from Snapcast
    const fetchData = useCallback(async () => {
        // Prevent concurrent fetches - use ref to avoid race conditions
        if (isFetchingRef.current) {
            return;
        }

        isFetchingRef.current = true;
        setConnectionError(null);

        try {
            const {initialStreams, initialClients, serverName: snapServerName} = await getSnapcastData();

            // Check grace period before updating streams
            const now = Date.now();
            const GRACE_PERIOD = 20000; // 20 second grace period (longer to account for D-Bus signal propagation)
            const recentChanges = recentUserChangesRef.current;
            const hasRecentChange = recentChanges && (now - recentChanges.timestamp) < GRACE_PERIOD;

            // Merge streams with grace period respect
            setStreams(prevStreams => {
                return initialStreams.map(newStream => {
                    const existingStream = prevStreams.find(s => s.id === newStream.id);

                    // Grace period for source volume changes (user-initiated from GUI)
                    if (existingStream && hasRecentChange && recentChanges!.type === 'sourceVolume' && recentChanges!.data.streamId === newStream.id) {
                        return {
                            ...newStream,
                            volume: recentChanges!.data.volume
                        };
                    }

                    // Grace period for seek operations (user-initiated from GUI)
                    if (existingStream && hasRecentChange && recentChanges!.type === 'seek' && recentChanges!.data.streamId === newStream.id) {
                        return {
                            ...newStream,
                            progress: recentChanges!.data.position
                        };
                    }

                    // Reject stale volume data: if we have a volume and new data doesn't match, prefer current
                    // This prevents cached stale data from overwriting fresh D-Bus signals
                    if (existingStream &&
                        existingStream.volume !== undefined &&
                        newStream.volume !== undefined &&
                        existingStream.volume !== newStream.volume) {

                        const lastKnown = volumeChangeTimestamps.current[newStream.id];
                        const VOLUME_CHANGE_GRACE = 3000; // 3 second grace to reject stale data

                        if (lastKnown && (now - lastKnown.timestamp) < VOLUME_CHANGE_GRACE) {
                            // Recent volume change - only accept if it matches what we know or is newer
                            if (newStream.volume === lastKnown.volume) {
                                // Polling caught up - accept it
                                return newStream;
                            } else {
                                // Stale data - reject it by keeping existing stream (preserves object identity to prevent re-renders)
                                return existingStream;
                            }
                        } else {
                            // No recent change or grace expired - accept and track
                            volumeChangeTimestamps.current[newStream.id] = {volume: newStream.volume, timestamp: now};
                            return newStream;
                        }
                    }

                    return newStream;
                });
            });

            setClients(initialClients);
            setServerName(snapServerName || 'Snapcast Server');

            // Build client to group mapping
            try {
                const serverStatus = await snapcastService.getServerStatus();
                const groupMap: Record<string, string> = {};

                if (serverStatus.server && serverStatus.server.groups) {
                    serverStatus.server.groups.forEach((group: any) => {
                        if (group.clients) {
                            group.clients.forEach((client: any) => {
                                groupMap[client.id] = group.id;
                            });
                        }
                    });
                }

                setClientGroupMap(groupMap);
            } catch (error) {
                console.error('[Init] Could not build client group mapping:', error);
            }

            // Load volume calibration settings
            try {
                const cals = await calibrationService.getAllCalibrations();
                setCalibrations(cals);
            } catch (error) {
                console.error('[Init] Could not load calibration settings:', error);
            }

            // Check if we got error data (connection failed)
            if (initialStreams.length === 1 && initialStreams[0].id === 'error-stream') {
                setConnectionError('Unable to connect to Snapcast server. Please check your configuration.');
            }
        } catch (error) {
            console.error('Error loading Snapcast data:', error);
            setConnectionError('Failed to load Snapcast data');

            // Set minimal fallback data
            setStreams([]);
            setClients([{
                id: 'client-1',
                name: 'My Device (You)',
                currentStreamId: null,
                volume: 75,
                connected: true,
            }]);
        } finally {
            // Always reset the flag, even if there was an error
            isFetchingRef.current = false;
        }
    }, []);

    // Initial data fetch on mount
    useEffect(() => {
        if (!isLoading) return; // Prevent multiple calls

        fetchData().finally(() => {
            setIsLoading(false);
        });
    }, []); // Empty dependency array - only run once

    // Poll for stream additions/removals (fallback for when Server.OnUpdate isn't sent)
    // This ensures dynamic streams appear/disappear even if WebSocket notifications fail
    useEffect(() => {
        if (!snapcastService) return;
        if (settings.federation.enabled) return; // Federation polling handles this

        const checkStreamList = async () => {
            try {
                const serverStatus = await snapcastService.getServerStatus();
                if (serverStatus && serverStatus.server && serverStatus.server.streams) {
                    const currentStreamIds = new Set(streamsRef.current.map(s => s.id));
                    const serverStreamIds = new Set(serverStatus.server.streams.map((s: any) => s.id));

                    // Check if streams were added or removed
                    const streamsAdded = [...serverStreamIds].some(id => !currentStreamIds.has(id));
                    const streamsRemoved = [...currentStreamIds].some(id => !serverStreamIds.has(id));

                    if (streamsAdded || streamsRemoved) {
                        if (!isFetchingRef.current) {
                            await fetchData();
                        }
                    }
                }
            } catch (error) {
                console.error('[StreamPoll] Error checking stream list:', error);
            }
        };

        // Poll every 10 seconds for stream list changes
        const interval = setInterval(checkStreamList, 10000);

        return () => clearInterval(interval);
    }, [settings.federation.enabled, fetchData]);

    // Federation polling - fetch data from federation API when enabled
    useEffect(() => {
        if (!settings.federation.enabled) {
            // Stop polling if federation was disabled
            federationService.stopPolling();
            setServers([]);
            return;
        }

        // Poll the backend until it's ready (with timeout)
        // The backend needs time to restart from minimal mode to full federation mode
        const startPollingDelayed = async () => {
            const maxAttempts = 20; // 20 attempts * 500ms = 10 seconds max
            let attempt = 0;
            let isReady = false;

            while (attempt < maxAttempts && !isReady) {
                attempt++;
                await new Promise(resolve => setTimeout(resolve, 500));

                // Check if federation service is healthy
                const healthy = await federationService.checkHealth();
                if (healthy) {
                    // Backend is healthy, now check if servers are connected
                    try {
                        const servers = await federationService.getServers();
                        const hasConnectedServers = servers.some(s => s.connected);

                        if (hasConnectedServers) {
                            isReady = true;
                        } else if (attempt === maxAttempts) {
                            console.error('[Federation] Backend did not connect to local server in time');
                            return; // Exit without starting polling
                        }
                    } catch (error) {
                        // Servers endpoint not ready yet, continue polling
                        if (attempt === maxAttempts) {
                            console.error('[Federation] Backend servers endpoint not ready in time');
                            return;
                        }
                    }
                } else if (attempt === maxAttempts) {
                    console.error('[Federation] Backend did not become ready in time');
                    return; // Exit without starting polling
                }
            }

            // Transform federated stream data to match frontend Stream type
            // Note: servers array from the same snapshot is used to derive serverName if not provided
            const transformFederatedStream = (federatedStream: any, snapshotServers: Server[]): Stream => {
                const metadata = federatedStream.metadata || {};
                const properties = federatedStream.properties || {};
                const playbackData = federatedStream.playback;

                // Extract stream name from federated ID by removing "{serverId}-" prefix
                // Example: "192-168-7-226-default" -> "default"
                const extractStreamName = (id: string, serverId: string): string => {
                    const prefix = `${serverId}-`;
                    if (id.startsWith(prefix)) {
                        return id.substring(prefix.length);
                    }
                    return 'Unknown';
                };

                const streamName = extractStreamName(federatedStream.id, federatedStream.serverId);

                // Derive serverName: prefer provided value, fallback to lookup from servers, then serverId
                let serverName = federatedStream.serverName;
                if (!serverName) {
                    // Try to find server name from the snapshot's servers list
                    const matchingServer = snapshotServers.find(s => s.id === federatedStream.serverId);
                    serverName = matchingServer?.name || federatedStream.serverId || 'Unknown Server';
                }

                // Build playback object if available from federation API
                const playback: PlaybackData | undefined = playbackData ? {
                    position: playbackData.position || 0,
                    duration: playbackData.duration || 0,
                    interpolated_position: playbackData.interpolated_position || 0,
                    playback_status: playbackData.playback_status || 'unknown',
                    is_stale: playbackData.is_stale ?? true
                } : undefined;

                return {
                    id: federatedStream.id,
                    serverId: federatedStream.serverId,
                    serverName: serverName,
                    name: streamName,
                    sourceDevice: streamName,
                    currentTrack: {
                        id: federatedStream.id,
                        title: metadata.title || 'Unknown Track',
                        artist: Array.isArray(metadata.artist) ? metadata.artist.join(', ') : (metadata.artist || 'Unknown Artist'),
                        album: metadata.album || 'Unknown Album',
                        albumArtUrl: metadata.artUrl ? (
                            metadata.artUrl.startsWith('/coverart/')
                                ? `http://${window.location.hostname}:5001/api/settings/proxy/coverart/${metadata.artUrl.replace('/coverart/', '')}`
                                : metadata.artUrl.startsWith('/')
                                    ? `http://${window.location.hostname}:1780${metadata.artUrl}`
                                    : metadata.artUrl
                        ) : musicNotePlaceholder,
                        duration: metadata.duration ? Math.floor(metadata.duration) : 0  // Already in seconds
                    },
                    isPlaying: properties.playbackStatus === 'playing',
                    progress: properties.position ? Math.floor(properties.position) : 0,  // Already in seconds (legacy)
                    playback,  // Server-provided playback position data
                    volume: typeof federatedStream.volume === 'number' ? federatedStream.volume : undefined  // Source volume from control script
                };
            };

            // Transform federated client data to ensure names are populated
            const transformFederatedClient = (federatedClient: any): Client => {
                // Extract client name from federated ID or use provided name
                // Federated client IDs may be in format "server-{serverId}-{clientId}"
                const extractClientName = (client: any): string => {
                    // If client has a name, use it
                    if (client.name && client.name !== '') {
                        return client.name;
                    }
                    // Otherwise try to extract from ID
                    // Format might be: "server-localhost-00:21:6a:7e:3f:ae" or similar
                    const parts = client.id.split('-');
                    if (parts.length >= 3) {
                        // Take everything after "server-{serverId}-"
                        return parts.slice(2).join('-');
                    }
                    return client.id; // Fallback to ID
                };

                return {
                    ...federatedClient,
                    name: extractClientName(federatedClient)
                };
            };

            // Helper to detect if a client ID is local (served by this server's WebSocket)
            // Local clients from federation API have "server-localhost-" prefix but should use WebSocket API
            const isLocalFederatedClient = (clientId: string): boolean => {
                return clientId.startsWith('server-localhost-');
            };

            // Helper to strip federation prefix from local client IDs
            const stripLocalPrefix = (clientId: string): string => {
                if (isLocalFederatedClient(clientId)) {
                    return clientId.replace('server-localhost-', '');
                }
                return clientId;
            };

            // Start polling for federated data
            federationService.startPolling((data) => {
                // Only update servers if we received data (prevent clearing on empty snapshots)
                if (data.servers.length > 0) {
                    setServers(data.servers);
                }

                // Also fetch active endpoint to determine current stream in multi-server mode
                federationService.getActiveEndpoint().then(endpoint => {
                    setActiveEndpoint(endpoint);
                }).catch(() => {
                    // Silently handle - active endpoint will be fetched on next poll
                });

                // Check if we should ignore polling updates due to recent user changes
                // Use ref to avoid stale closure
                const now = Date.now();
                const GRACE_PERIOD = 20000; // 20 second grace period for user changes (accounts for D-Bus signal propagation delay)
                const recentChanges = recentUserChangesRef.current;
                const hasRecentChange = recentChanges && (now - recentChanges.timestamp) < GRACE_PERIOD;

                // Transform and MERGE federated streams (preserve client-side progress tracking)
                if (data.streams.length > 0) {
                    setStreams(prevStreams => {
                        const transformedStreams = data.streams.map(federatedStream => {
                            const newStream = transformFederatedStream(federatedStream, data.servers);

                            // Find existing stream to preserve client-side progress and serverName
                            const existingStream = prevStreams.find(s => s.id === newStream.id);

                            // CRITICAL: Preserve serverName from existing stream if new one is undefined/empty
                            // This prevents "Unknown Server" flash during backend state transitions
                            if (existingStream && existingStream.serverName && !newStream.serverName) {
                                newStream.serverName = existingStream.serverName;
                            }

                            // CRITICAL: Grace period check FIRST - applies regardless of isPlaying state
                            // This prevents server state from overwriting user's recent play/pause action
                            if (existingStream && hasRecentChange && recentChanges!.type === 'playback' && recentChanges!.data.streamId === newStream.id) {
                                // User recently changed playback state - preserve their action
                                // Use existing progress to avoid resetting on pause
                                return {
                                    ...newStream,
                                    isPlaying: existingStream.isPlaying,
                                    progress: existingStream.progress
                                };
                            }

                            // Grace period for source volume changes (user-initiated from GUI)
                            if (existingStream && hasRecentChange && recentChanges!.type === 'sourceVolume' && recentChanges!.data.streamId === newStream.id) {
                                // User recently changed source volume - preserve their action
                                return {
                                    ...newStream,
                                    volume: recentChanges!.data.volume
                                };
                            }

                            // Grace period for seek operations (user-initiated from GUI)
                            if (existingStream && hasRecentChange && recentChanges!.type === 'seek' && recentChanges!.data.streamId === newStream.id) {
                                // User recently seeked - preserve their seek position
                                return {
                                    ...newStream,
                                    progress: recentChanges!.data.position
                                };
                            }

                            // Reject stale volume data from ANY source (not just user-initiated)
                            if (existingStream &&
                                existingStream.volume !== undefined &&
                                newStream.volume !== undefined &&
                                existingStream.volume !== newStream.volume) {

                                const lastKnown = volumeChangeTimestamps.current[newStream.id];
                                const VOLUME_CHANGE_GRACE = 3000; // 3 second grace to reject stale data

                                if (lastKnown && (now - lastKnown.timestamp) < VOLUME_CHANGE_GRACE) {
                                    // Recent volume change - only accept if it matches what we know or is newer
                                    if (newStream.volume === lastKnown.volume) {
                                        // Polling caught up - accept it
                                        return newStream;
                                    } else {
                                        // Stale data - reject it by keeping existing stream (preserves object identity to prevent re-renders)
                                        return existingStream;
                                    }
                                } else {
                                    // No recent change or grace expired - accept and track
                                    volumeChangeTimestamps.current[newStream.id] = {volume: newStream.volume, timestamp: now};
                                    return newStream;
                                }
                            }

                            if (existingStream && existingStream.isPlaying) {
                                // Stream is playing - preserve client-side progress for smooth updates
                                // Accept all other server data (metadata, isPlaying, etc.)
                                // Use whichever progress is higher (client increments between polls)
                                return {
                                    ...newStream,
                                    progress: Math.max(newStream.progress, existingStream.progress)
                                };
                            }

                            // New stream or not playing (without recent user action) - use server data as-is
                            return newStream;
                        });

                        return transformedStreams;
                    });
                }

                // Transform and MERGE federated clients (prevent temporary disappearance during backend state changes)
                if (data.clients.length > 0) {
                    const transformedClients = data.clients.map(client => {
                        const transformed = transformFederatedClient(client);

                        if (hasRecentChange && recentChanges!.type === 'routing' && recentChanges!.data.clientId === transformed.id) {
                            return {
                                ...transformed,
                                currentStreamId: recentChanges!.data.streamId
                            };
                        }

                        return transformed;
                    });

                    // Update last seen timestamps for all clients in this snapshot
                    const newClientIds = new Set(transformedClients.map(c => c.id));
                    transformedClients.forEach(client => {
                        clientLastSeenRef.current[client.id] = now;
                    });

                    // Merge: keep existing clients that were recently seen but missing from this snapshot
                    // This prevents clients from disappearing during transient backend state changes
                    setClients(prevClients => {
                        // Start with all transformed clients from the snapshot
                        const mergedClients = [...transformedClients];

                        // Check for any previously-existing clients that are missing from this snapshot
                        prevClients.forEach(existingClient => {
                            if (!newClientIds.has(existingClient.id)) {
                                // Client is missing from this snapshot - check if we should retain it
                                const lastSeen = clientLastSeenRef.current[existingClient.id];
                                const retentionExpired = !lastSeen || (now - lastSeen) > CLIENT_RETENTION_GRACE_MS;

                                if (!retentionExpired) {
                                    // Client was recently seen - keep it to prevent flickering
                                    mergedClients.push(existingClient);
                                }
                                // If retention expired, the client is truly gone - don't add it back
                            }
                        });

                        return mergedClients;
                    });
                }
            }, 5000);
        };

        // Call the async function to start polling after delay
        startPollingDelayed();

        return () => {
            federationService.stopPolling();
        };
    }, [settings.federation.enabled]);

    const handleVolumeChange = async (clientId: string, volume: number) => {
        // Handle browser audio client volume changes locally
        // Compare with effective browser client ID (accounting for federation prefix)
        const effectiveBrowserClientId = getBrowserAudioClientId();
        if (clientId === effectiveBrowserClientId) {
            browserAudio.setVolume(volume);
            // Also update clients state to ensure controlled input stays in sync
            // (The allClients mapping will override with browserAudio.state.volume,
            // but this ensures React's controlled input batching works correctly)
            setClients(prevClients =>
                prevClients.map(c => (c.id === clientId ? {...c, volume} : c))
            );
            return;
        }

        // Check if this is a local hardware client being controlled while federated to remote server
        // If so, redirect volume control to the remote snapclient that's actually playing audio
        if (settings.federation.enabled && activeEndpoint.active && activeEndpoint.serverId && activeEndpoint.clientId) {
            // Check if the client being controlled is a local hardware client (MAC address format)
            const isMacAddress = /^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$/.test(clientId);

            // ONLY redirect if this is a local hardware client (MAC address)
            // Do NOT redirect for federated clients (they're already on the remote server)
            if (isMacAddress) {
                // Build the federated client ID for the remote snapclient
                const remoteClientId = `${activeEndpoint.serverId}-${activeEndpoint.clientId}`;

                // Update local state for the local client (for UI responsiveness)
                setClients(prevClients =>
                    prevClients.map(c => (c.id === clientId ? {...c, volume} : c))
                );

                // Send volume command to remote snapclient via federation API
                try {
                    await federationService.setVolume(remoteClientId, volume);
                    return;
                } catch (error) {
                    console.error(`Failed to set volume for remote snapclient ${remoteClientId}:`, error);
                    // Fall through to try local control as fallback
                }
            }
        }

        // Update local state immediately for responsiveness
        setClients(prevClients =>
            prevClients.map(c => (c.id === clientId ? {...c, volume} : c))
        );

        // Determine if this is a local federated client or remote federated client
        const isLocalFederated = clientId.startsWith('server-localhost-');
        const isRemoteFederated = settings.federation.enabled && clientId.startsWith('server-') && !isLocalFederated;

        // If federation is enabled and client is REMOTE federated, use federation API
        if (isRemoteFederated) {
            try {
                await federationService.setVolume(clientId, volume);
            } catch (error) {
                console.error(`Failed to set volume for federated client ${clientId}:`, error);
            }
            return;
        }

        // Local client (either direct WebSocket or local federated) - use WebSocket API
        // Strip "server-localhost-" prefix if present
        const localClientId = isLocalFederated ? clientId.replace('server-localhost-', '') : clientId;

        try {
            await snapcastService.setClientVolume(localClientId, volume);
        } catch (error) {
            console.error(`Failed to set volume for client ${localClientId}:`, error);
            // Could revert local state here if needed
        }
    };

    // Handle source/integration volume changes (AirPlay, Spotify, etc.)
    const handleSourceVolumeChange = async (streamId: string, volume: number) => {
        console.log(`[handleSourceVolumeChange] Called with streamId='${streamId}', volume=${volume}`);

        // Track user change with grace period to prevent polling from overwriting
        const timestamp = Date.now();
        const userChange = {type: 'sourceVolume', timestamp, data: {streamId, volume}};
        recentUserChangesRef.current = userChange;
        setRecentUserChanges(userChange);
        // Also track in volumeChangeTimestamps to reject stale polling data
        volumeChangeTimestamps.current[streamId] = {volume, timestamp};
        console.log(`[VolumeGrace] Set grace period for volume=${volume}%, expires in 7s`);

        // Update local stream state immediately for responsiveness
        setStreams(prevStreams =>
            prevStreams.map(s => (s.id === streamId ? {...s, volume} : s))
        );

        // Determine if this is a remote federated stream
        // Pattern: server-192-168-X-X-streamname (but NOT server-localhost-*)
        const isLocalFederated = streamId.startsWith('server-localhost-');
        const isRemoteFederated = settings.federation.enabled && streamId.startsWith('server-') && !isLocalFederated;

        try {
            if (isRemoteFederated) {
                // Remote federated stream - use federation API to forward to remote server
                console.log(`[handleSourceVolumeChange] Remote federated stream, using federation API`);
                const result = await federationService.setStreamVolume(streamId, volume);
                if (!result.success) {
                    console.error(`Failed to set source volume via federation: ${result.message}`);
                    // Clear the grace period on failure
                    console.log(`[VolumeGrace] Cleared grace period due to federation API failure`);
                    recentUserChangesRef.current = null;
                    setRecentUserChanges(null);
                } else {
                    console.log(`[handleSourceVolumeChange] Successfully set volume to ${volume} for remote stream ${streamId}`);
                }
            } else {
                // Local stream - use direct audio API with D-Bus MPRIS control
                // Strip the server-localhost- prefix if present
                const localStreamId = isLocalFederated ? streamId.replace('server-localhost-', '') : streamId;
                const payload = { streamId: localStreamId, volume };
                console.log(`[handleSourceVolumeChange] Local stream, sending payload:`, payload);

                const response = await fetch('/api/audio/source-volume', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify(payload),
                });

                if (!response.ok) {
                    const error = await response.json();
                    console.error(`Failed to set source volume: ${error.message || error.error}`);
                    // Clear the grace period on failure
                    console.log(`[VolumeGrace] Cleared grace period due to API failure`);
                    recentUserChangesRef.current = null;
                    setRecentUserChanges(null);
                } else {
                    console.log(`[handleSourceVolumeChange] Successfully set volume to ${volume} for stream ${streamId}`);
                }
            }
        } catch (error) {
            console.error(`Failed to set source volume for stream ${streamId}:`, error);
            // Clear the grace period on error
            console.log(`[VolumeGrace] Cleared grace period due to error`);
            recentUserChangesRef.current = null;
            setRecentUserChanges(null);
        }
    };

    const handleGroupVolumeAdjust = (streamId: string | null, direction: 'up' | 'down') => {
        if (!streamId) return;
        const adjustment = direction === 'up' ? VOLUME_STEP : -VOLUME_STEP;
        const effectiveBrowserClientId = getBrowserAudioClientId();

        setClients(prevClients => {
            const updatedClients = prevClients.map(c => {
                if (c.currentStreamId === streamId) {
                    const newVolume = Math.max(0, Math.min(100, c.volume + adjustment));

                    // Handle browser audio client locally
                    if (c.id === effectiveBrowserClientId) {
                        browserAudio.setVolume(newVolume);
                    } else {
                        // Determine if remote federated or local
                        const isLocalFederated = c.id.startsWith('server-localhost-');
                        const isRemoteFederated = settings.federation.enabled && c.id.startsWith('server-') && !isLocalFederated;

                        if (isRemoteFederated) {
                            // Remote federated client - use federation API
                            federationService.setVolume(c.id, newVolume).catch(error => {
                                console.error(`Failed to adjust volume for federated client ${c.id}:`, error);
                            });
                        } else {
                            // Local client - use WebSocket API (strip prefix if needed)
                            const localClientId = isLocalFederated ? c.id.replace('server-localhost-', '') : c.id;
                            snapcastService.setClientVolume(localClientId, newVolume).catch(error => {
                                console.error(`Failed to adjust volume for client ${localClientId}:`, error);
                            });
                        }
                    }

                    return {...c, volume: newVolume};
                }
                return c;
            });

            return updatedClients;
        });
    };

    const handleGroupMute = (streamId: string | null) => {
        if (!streamId) return;

        const isMuted = preMuteGroupVolumes[streamId];
        const effectiveBrowserClientId = getBrowserAudioClientId();

        if (isMuted) {
            setClients(prevClients =>
                prevClients.map(c => {
                    if (c.currentStreamId === streamId && preMuteGroupVolumes[streamId][c.id] !== undefined) {
                        const restoredVolume = preMuteGroupVolumes[streamId][c.id];

                        // Handle browser audio client locally
                        if (c.id === effectiveBrowserClientId) {
                            browserAudio.setVolume(restoredVolume, false);
                        } else {
                            // Determine if remote federated or local
                            const isLocalFederated = c.id.startsWith('server-localhost-');
                            const isRemoteFederated = settings.federation.enabled && c.id.startsWith('server-') && !isLocalFederated;

                            if (isRemoteFederated) {
                                // Remote federated client - use federation API
                                federationService.setVolume(c.id, restoredVolume, false).catch(error => {
                                    console.error(`Failed to unmute federated client ${c.id}:`, error);
                                });
                            } else {
                                // Local client - unmute on server (strip prefix if needed)
                                const localClientId = isLocalFederated ? c.id.replace('server-localhost-', '') : c.id;
                                snapcastService.setClientVolume(localClientId, restoredVolume, false).catch(error => {
                                    console.error(`Failed to unmute client ${localClientId}:`, error);
                                });
                            }
                        }

                        return {...c, volume: restoredVolume};
                    }
                    return c;
                })
            );
            setPreMuteGroupVolumes(prev => {
                const newPreMuteVolumes = {...prev};
                delete newPreMuteVolumes[streamId];
                return newPreMuteVolumes;
            });
        } else {
            const volumesToStore: Record<string, number> = {};
            clients.forEach(c => {
                if (c.currentStreamId === streamId) {
                    volumesToStore[c.id] = c.volume;
                }
            });

            if (Object.keys(volumesToStore).length > 0) {
                setPreMuteGroupVolumes(prev => ({
                    ...prev,
                    [streamId]: volumesToStore,
                }));
            }

            setClients(prevClients =>
                prevClients.map(c => {
                    if (c.currentStreamId === streamId) {
                        // Handle browser audio client locally
                        if (c.id === effectiveBrowserClientId) {
                            browserAudio.setVolume(0, true);
                        } else {
                            // Determine if remote federated or local
                            const isLocalFederated = c.id.startsWith('server-localhost-');
                            const isRemoteFederated = settings.federation.enabled && c.id.startsWith('server-') && !isLocalFederated;

                            if (isRemoteFederated) {
                                // Remote federated client - use federation API
                                federationService.setVolume(c.id, 0, true).catch(error => {
                                    console.error(`Failed to mute federated client ${c.id}:`, error);
                                });
                            } else {
                                // Local client - mute on server (strip prefix if needed)
                                const localClientId = isLocalFederated ? c.id.replace('server-localhost-', '') : c.id;
                                snapcastService.setClientVolume(localClientId, 0, true).catch(error => {
                                    console.error(`Failed to mute client ${localClientId}:`, error);
                                });
                            }
                        }

                        return {...c, volume: 0};
                    }
                    return c;
                })
            );
        }
    };

    const handleStreamChange = async (clientId: string, streamId: string | null) => {
        // Handle browser audio client - stop it when stream set to null
        // Compare with effective browser client ID (accounting for federation prefix)
        const effectiveBrowserClientId = getBrowserAudioClientId();
        if (clientId === effectiveBrowserClientId && streamId === null) {
            browserAudio.stop();
            return;
        }

        // IMPORTANT: Capture the ORIGINAL stream BEFORE optimistic update
        // We need this to determine if client is currently on a remote stream
        const currentClient = clients.find(c => c.id === clientId);
        const originalStreamId = currentClient?.currentStreamId;
        const isCurrentStreamRemote = originalStreamId ? !isLocalId(originalStreamId) : false;

        // Update local state immediately for responsiveness
        setClients(prevClients =>
            prevClients.map(c => (c.id === clientId ? {...c, currentStreamId: streamId} : c))
        );

        // Track this as a user-initiated change to prevent polling from overwriting it
        const timestamp = Date.now();
        setRecentUserChanges({type: 'routing', timestamp, data: {clientId, streamId}});

        // Check if this is a local client (use WebSocket) or remote client (use Federation API)
        const isClientLocal = isLocalId(clientId);
        const isStreamLocal = streamId ? isLocalId(streamId) : true;
        const localServer = getLocalServer();

        // Check if browser audio client needs to switch servers for remote streams
        const isBrowserClient = clientId === browserClientId || clientId === getBrowserAudioClientId();
        if (settings.federation.enabled && isBrowserClient && streamId) {
            const newServer = getServerForStream(streamId);
            const newHost = newServer?.host || window.location.hostname;
            const currentHost = browserAudio.state.currentHost;

            // If browser client is switching to a stream on a different server, restart connection
            if (currentHost && newHost !== currentHost) {
                console.log(`[StreamChange] Browser audio switching from ${currentHost} to ${newHost}`);
                browserAudio.restart(newHost, browserAudio.state.muted, newServer?.port || 1780);
            }
        }

        // Use federation API for:
        // 1. Remote clients (any stream)
        // 2. Local hardware clients accessing remote streams (snapclient redirection)
        // 3. Local hardware clients CURRENTLY on remote streams (need to disconnect via federation)
        const needsFederationRouting = settings.federation.enabled && (
            !isClientLocal || // Remote client
            (!isStreamLocal && !isBrowserClient) || // Local hardware client + remote stream
            (isCurrentStreamRemote && !isBrowserClient) // Local hardware client currently on remote stream (disconnecting)
        );

        if (needsFederationRouting) {
            // If streamId is null, convert it to the CLIENT's server's none stream
            // This is important: when routing a remote client to none, we need THAT client's
            // server's none stream, not the local server's none stream
            let targetStreamId = streamId;
            if (!streamId || streamId === null) {
                // Extract the client's server ID from the clientId
                // clientId format: "server-192-168-7-122-<client-id>"
                // We need to extract "server-192-168-7-122"
                const clientServerMatch = clientId.match(/^(server-[\d-]+)-/);
                const clientServerId = clientServerMatch ? clientServerMatch[1] : null;

                if (!clientServerId) {
                    console.error('[StreamChange] Could not extract server ID from clientId:', clientId);
                    return;
                }

                // Find the none stream for the CLIENT's server (not local server!)
                const noneStream = streams.find(s =>
                    s.serverId === clientServerId && s.id.includes('none-')
                );

                if (noneStream) {
                    targetStreamId = noneStream.id;
                } else {
                    console.error('[StreamChange] Could not find none stream for server:', clientServerId);
                    return;
                }
            }

            try {
                const result = await federationService.routeClient(clientId, targetStreamId);

                if (result.success) {
                    // Trigger immediate poll to refresh UI with backend state
                    await federationService.triggerPoll();

                    // Apply dB-matched volume if joining a stream (not null/none)
                    if (targetStreamId && !targetStreamId.includes('none-')) {
                        await applyDbMatchedVolume(clientId, targetStreamId);
                    }
                } else {
                    console.error('[StreamChange] Routing failed:', result.message);
                    // Revert local state on error
                    setClients(prevClients =>
                        prevClients.map(c => (c.id === clientId ? {...c, currentStreamId: c.currentStreamId} : c))
                    );
                }
            } catch (error) {
                console.error(`Failed to route federated client ${clientId}:`, error);
                // Revert local state on error
                setClients(prevClients =>
                    prevClients.map(c => (c.id === clientId ? {...c, currentStreamId: c.currentStreamId} : c))
                );
            }
            return;
        }

        // Local client - use WebSocket API
        // Strip server prefix if present (e.g., "server-192-168-7-122-")
        const localClientId = stripServerPrefix(clientId);
        const localStreamId = streamId ? stripServerPrefix(streamId) : null;

        try {
            let groupId = clientGroupMap[localClientId];

            // If group not in cache, fetch it dynamically (handles newly connected clients)
            if (!groupId) {
                console.log(`[StreamChange] Group not in cache for ${localClientId}, fetching from server...`);
                try {
                    const serverStatus = await snapcastService.getServerStatus();
                    if (serverStatus.server && serverStatus.server.groups) {
                        for (const group of serverStatus.server.groups) {
                            if (group.clients) {
                                const foundClient = group.clients.find((c: any) => c.id === localClientId);
                                if (foundClient) {
                                    groupId = group.id;
                                    // Update the cache for future calls
                                    setClientGroupMap(prev => ({...prev, [localClientId]: group.id}));
                                    console.log(`[StreamChange] Found group ${groupId} for client ${localClientId}`);
                                    break;
                                }
                            }
                        }
                    }
                } catch (fetchError) {
                    console.error(`[StreamChange] Failed to fetch server status:`, fetchError);
                }
            }

            if (groupId && localStreamId) {
                await snapcastService.setGroupStream(groupId, localStreamId);

                // Apply dB-matched volume when joining a stream
                if (streamId) {
                    await applyDbMatchedVolume(clientId, streamId);
                }
            } else if (groupId && localStreamId === null) {
                // streamId is null - handled via local state update only
            } else {
                console.error(`[StreamChange] Could not find group for client ${localClientId}`);
            }
        } catch (error) {
            console.error(`Failed to change stream for client ${localClientId}:`, error);

            // Revert local state on error
            setClients(prevClients =>
                prevClients.map(c => (c.id === clientId ? {...c, currentStreamId: c.currentStreamId} : c))
            );
        }
    };

    // Helper to check if a stream is local (belongs to this server)
    const isLocalStream = (stream: Stream): boolean => {
        if (!settings.federation.enabled) return true;

        // In federation mode, check if the stream's server is the local server
        if (!stream.serverId) return true;

        // Local server is identified by serverId containing "localhost" or being marked as local
        const localServer = servers.find(s => s.isLocal || s.id.includes('localhost'));
        return stream.serverId === localServer?.id;
    };

    // Helper to get local stream ID (strip federation prefix if present)
    const getLocalStreamId = (streamId: string): string => {
        if (!settings.federation.enabled) return streamId;

        // Strip any "server-{ip}-" or "server-localhost-" prefix
        const match = streamId.match(/^server-[\d-]+-(.+)$/) || streamId.match(/^server-localhost-(.+)$/);
        return match ? match[1] : streamId;
    };

    // Helper to map WebSocket stream ID to federated stream ID
    // WebSocket sends: "Airplay" → we need to find: "server-192-168-201-133-Airplay"
    const getFederatedStreamId = (localStreamId: string): string => {
        if (!settings.federation.enabled) return localStreamId;

        // Find the local server
        const localServer = servers.find(s => s.isLocal || s.id.includes('localhost'));
        if (!localServer) return localStreamId; // Fallback if server not found yet

        // Construct federated ID: server ID + local stream ID
        return `${localServer.id}-${localStreamId}`;
    };

    /**
     * Apply dB-matched volume when a client joins a stream.
     * Finds existing clients on the stream, calculates matching volume using calibration data,
     * and sets the joining client's volume to match the dB level.
     */
    const applyDbMatchedVolume = useCallback(async (joiningClientId: string, targetStreamId: string) => {
        // Don't apply if no calibrations loaded
        if (Object.keys(calibrations).length === 0) {
            return;
        }

        // Find existing clients on the target stream (excluding the joining client)
        const existingClientsOnStream = clients.filter(c =>
            c.currentStreamId === targetStreamId &&
            c.id !== joiningClientId &&
            c.connected
        );

        if (existingClientsOnStream.length === 0) {
            // No other clients on stream - nothing to match against
            // Could apply default volume from calibration here
            const joiningCal = calibrations[joiningClientId] || calibrations[stripServerPrefix(joiningClientId)];
            if (joiningCal && joiningCal.defaultVolume !== undefined) {
                console.log(`[dB-Match] No reference clients, applying default volume ${joiningCal.defaultVolume}% for ${joiningClientId}`);
                // Set the default volume
                const localClientId = stripServerPrefix(joiningClientId);
                try {
                    await snapcastService.setClientVolume(localClientId, joiningCal.defaultVolume);
                } catch (error) {
                    console.error('[dB-Match] Failed to set default volume:', error);
                }
            }
            return;
        }

        // Find a reference client - prefer calibrated ones
        let referenceClient = existingClientsOnStream.find(c => {
            const cal = calibrations[c.id] || calibrations[stripServerPrefix(c.id)];
            return cal?.calibrated;
        });

        // Fall back to first client if no calibrated ones
        if (!referenceClient) {
            referenceClient = existingClientsOnStream[0];
        }

        // Get calibrations for both clients
        const refCal = calibrations[referenceClient.id] || calibrations[stripServerPrefix(referenceClient.id)];
        const joiningCal = calibrations[joiningClientId] || calibrations[stripServerPrefix(joiningClientId)];

        // Both clients need calibration for dB matching
        if (!refCal?.calibrated || !joiningCal?.calibrated) {
            console.log('[dB-Match] Skipping - one or both clients not calibrated');
            // If joining client has a default, use that
            if (joiningCal?.defaultVolume !== undefined) {
                const localClientId = stripServerPrefix(joiningClientId);
                try {
                    await snapcastService.setClientVolume(localClientId, joiningCal.defaultVolume);
                } catch (error) {
                    console.error('[dB-Match] Failed to set default volume:', error);
                }
            }
            return;
        }

        // Calculate matching volume
        const matchingVolume = calibrationService.getMatchingSliderVolume(
            referenceClient.volume,
            refCal,
            joiningCal
        );

        if (matchingVolume !== null && matchingVolume !== referenceClient.volume) {
            console.log(`[dB-Match] Setting ${joiningClientId} volume to ${matchingVolume}% to match ${referenceClient.name} at ${referenceClient.volume}%`);

            // Update local state
            setClients(prevClients =>
                prevClients.map(c => c.id === joiningClientId ? {...c, volume: matchingVolume} : c)
            );

            // Apply to Snapcast
            const localClientId = stripServerPrefix(joiningClientId);
            try {
                await snapcastService.setClientVolume(localClientId, matchingVolume);
            } catch (error) {
                console.error('[dB-Match] Failed to set matched volume:', error);
            }
        }
    }, [clients, calibrations]);

    // Track last play/pause command time to debounce rapid toggling
    // This prevents FIFO pipe issues when pause/play are sent too quickly
    const lastPlayPauseRef = useRef<number>(0);
    const PLAY_PAUSE_DEBOUNCE_MS = 2000; // 2 second minimum between play/pause commands

    const handlePlayPause = async () => {
        if (!currentStream) return;

        // Debounce: prevent rapid play/pause toggling which can break FIFO pipes
        const now = Date.now();
        if (now - lastPlayPauseRef.current < PLAY_PAUSE_DEBOUNCE_MS) {
            return; // Debounced
        }
        lastPlayPauseRef.current = now;

        try {
            const command = currentStream.isPlaying ? 'pause' : 'play';

            // Check if this is a local or remote stream
            if (settings.federation.enabled && !isLocalStream(currentStream)) {
                // Remote stream - use federation API

                // Optimistically update state
                setStreams(prevStreams =>
                    prevStreams.map(s =>
                        s.id === currentStream.id ? {...s, isPlaying: !currentStream.isPlaying} : s
                    )
                );
                const timestamp = Date.now();
                const playbackChange = {streamId: currentStream.id, timestamp};
                const userChange = {type: 'playback', timestamp, data: {streamId: currentStream.id}};
                // Update refs immediately for WebSocket and federation polling handlers
                recentPlaybackChangeRef.current = playbackChange;
                recentUserChangesRef.current = userChange;
                setRecentPlaybackChange(playbackChange);
                setRecentUserChanges(userChange);

                const result = await federationService.controlStream(currentStream.id, command);
                if (!result.success) {
                    console.error(`Federation playback control failed: ${result.message}`);
                    recentPlaybackChangeRef.current = null;
                    setRecentPlaybackChange(null);
                    setRecentUserChanges(null);
                }
            } else {
                // Local stream - use WebSocket API
                const localStreamId = getLocalStreamId(currentStream.id);

                // Check stream capabilities first
                const capabilities = await snapcastService.getStreamCapabilities(localStreamId);

                if (currentStream.isPlaying) {
                    // Try to pause
                    if (capabilities.canPause) {
                        setStreams(prevStreams =>
                            prevStreams.map(s =>
                                s.id === currentStream.id ? {...s, isPlaying: false} : s
                            )
                        );
                        const timestamp = Date.now();
                        const playbackChange = {streamId: currentStream.id, timestamp};
                        const userChange = {type: 'playback', timestamp, data: {streamId: currentStream.id}};
                        // Update refs immediately for WebSocket and federation polling handlers
                        recentPlaybackChangeRef.current = playbackChange;
                        recentUserChangesRef.current = userChange;
                        setRecentPlaybackChange(playbackChange);
                        setRecentUserChanges(userChange);
                        await snapcastService.pauseStream(localStreamId);

                        // Safety fallback: If no WebSocket confirmation arrives within 15s, query actual state
                        setTimeout(async () => {
                            const current = streamsRef.current.find(s => s.id === currentStream.id);
                            if (current && !current.isPlaying) {
                                // Still showing paused - verify with backend
                                const serverStatus = await snapcastService.getServerStatus();
                                const serverStream = serverStatus?.server?.streams?.find((s: any) =>
                                    s.id === getLocalStreamId(currentStream.id)
                                );
                                if (serverStream) {
                                    const actualIsPlaying = snapcastService.isStreamPlaying(serverStream);
                                    if (actualIsPlaying !== current.isPlaying) {
                                        setStreams(prev => prev.map(s =>
                                            s.id === currentStream.id ? {...s, isPlaying: actualIsPlaying} : s
                                        ));
                                    }
                                }
                            }
                        }, 15000);
                    } else {
                        console.warn(`Stream ${currentStream.id} does not support pause`);
                    }
                } else {
                    // Try to play
                    if (capabilities.canPlay) {
                        setStreams(prevStreams =>
                            prevStreams.map(s =>
                                s.id === currentStream.id ? {...s, isPlaying: true} : s
                            )
                        );
                        const timestamp = Date.now();
                        const playbackChange = {streamId: currentStream.id, timestamp};
                        const userChange = {type: 'playback', timestamp, data: {streamId: currentStream.id}};
                        // Update refs immediately for WebSocket and federation polling handlers
                        recentPlaybackChangeRef.current = playbackChange;
                        recentUserChangesRef.current = userChange;
                        setRecentPlaybackChange(playbackChange);
                        setRecentUserChanges(userChange);
                        await snapcastService.playStream(localStreamId);

                        // Safety fallback: If no WebSocket confirmation arrives within 15s, query actual state
                        setTimeout(async () => {
                            const current = streamsRef.current.find(s => s.id === currentStream.id);
                            if (current && current.isPlaying) {
                                // Still showing playing - verify with backend
                                const serverStatus = await snapcastService.getServerStatus();
                                const serverStream = serverStatus?.server?.streams?.find((s: any) =>
                                    s.id === getLocalStreamId(currentStream.id)
                                );
                                if (serverStream) {
                                    const actualIsPlaying = snapcastService.isStreamPlaying(serverStream);
                                    if (actualIsPlaying !== current.isPlaying) {
                                        setStreams(prev => prev.map(s =>
                                            s.id === currentStream.id ? {...s, isPlaying: actualIsPlaying} : s
                                        ));
                                    }
                                }
                            }
                        }, 15000);
                    } else {
                        console.warn(`Stream ${currentStream.id} does not support play`);
                    }
                }
            }
        } catch (error) {
            console.error(`Playback control failed for stream ${currentStream.id}:`, error);
            recentPlaybackChangeRef.current = null;
            setRecentPlaybackChange(null);
        }
    };

    const handleSkip = async (direction: 'next' | 'prev') => {
        if (!currentStream) return;

        // Optimistically clear artwork IMMEDIATELY when user clicks skip
        const defaultArtwork = musicNotePlaceholder;

        setStreams(prevStreams =>
            prevStreams.map(s =>
                s.id === currentStream.id
                    ? {...s, currentTrack: {...s.currentTrack, albumArtUrl: defaultArtwork}}
                    : s
            )
        );

        try {
            const command = direction === 'next' ? 'next' : 'previous';

            // Check if this is a local or remote stream
            if (settings.federation.enabled && !isLocalStream(currentStream)) {
                // Remote stream - use federation API
                const result = await federationService.controlStream(currentStream.id, command);
                if (!result.success) {
                    console.error(`Federation skip failed: ${result.message}`);
                }
            } else {
                // Local stream - use WebSocket API
                const localStreamId = getLocalStreamId(currentStream.id);
                const capabilities = await snapcastService.getStreamCapabilities(localStreamId);

                if (direction === 'next') {
                    if (capabilities.canGoNext) {
                        await snapcastService.nextTrack(localStreamId);
                    } else {
                        console.warn(`Stream ${currentStream.id} does not support next track`);
                    }
                } else {
                    if (capabilities.canGoPrevious) {
                        await snapcastService.previousTrack(localStreamId);
                    } else {
                        console.warn(`Stream ${currentStream.id} does not support previous track`);
                    }
                }
            }
        } catch (error) {
            console.error(`Skip ${direction} failed for stream ${currentStream.id}:`, error);
        }
    };

    const handleSeek = async (positionInSeconds: number) => {
        if (!currentStream) return;

        try {
            // Seek is only supported for local streams (no federation API endpoint)
            if (settings.federation.enabled && !isLocalStream(currentStream)) {
                console.warn(`Seek not supported for remote streams in federation mode`);
                return;
            }

            const localStreamId = getLocalStreamId(currentStream.id);
            const capabilities = await snapcastService.getStreamCapabilities(localStreamId);

            if (capabilities.canSeek) {
                const positionInMs = positionInSeconds * 1000;

                // Set grace period to prevent polling from overwriting the seek position
                const timestamp = Date.now();
                const userChange = {type: 'seek', timestamp, data: {streamId: currentStream.id, position: positionInSeconds}};
                recentUserChangesRef.current = userChange;
                setRecentUserChanges(userChange);
                console.log(`[SeekGrace] Set grace period for position=${positionInSeconds}s, expires in 7s`);

                updateStreamProgress(currentStream.id, positionInSeconds);
                await snapcastService.seekTo(localStreamId, positionInMs);
            } else {
                console.warn(`Stream ${currentStream.id} does not support seek`);
            }
        } catch (error) {
            console.error(`Seek failed for stream ${currentStream.id}:`, error);
        }
    };

    if (isLoading) {
        return (
            <div className="flex items-center justify-center min-h-screen bg-[var(--bg-primary)]">
                <div className="text-center">
                    <Icon name="spinner" spin className="text-5xl text-[var(--accent-color)]" />
                    <p className="mt-4 text-lg text-[var(--text-secondary)]">Connecting to Snapcast Server...</p>
                    <p className="mt-2 text-sm text-[var(--text-muted)]">{serverName}</p>
                </div>
            </div>
        );
    }

    if (!myClient) {
        return (
            <div className="flex items-center justify-center min-h-screen bg-[var(--bg-primary)]">
                <div className="text-center">
                    <Icon name="triangle-exclamation" className="text-5xl text-red-400" />
                    <p className="mt-4 text-lg text-[var(--text-secondary)]">No clients found</p>
                    <p className="mt-2 text-sm text-[var(--text-muted)]">Unable to load client data from Snapcast
                        server</p>
                </div>
            </div>
        );
    }

    // Toggle visualizer
    const toggleVisualizer = () => {
        setIsVisualizerOpen(!isVisualizerOpen);
    };

    return (
        <>
            <Routes>
                {/* Main UI Route */}
                <Route path="/" element={
                <div
                    className="min-h-screen bg-[var(--bg-primary)] text-[var(--text-primary)] font-sans p-4 md:p-8 flex flex-col">
                    {connectionError && (
                        <div className="w-full max-w-7xl mx-auto mb-4 p-4 bg-red-600/20 border border-red-600/30 rounded-lg">
                            <div className="flex items-center">
                                <Icon name="triangle-exclamation" className="text-red-400 mr-2" />
                                <span className="text-red-400">{connectionError}</span>
                            </div>
                        </div>
                    )}

            <div className="w-full max-w-7xl mx-auto flex-grow grid grid-cols-1 lg:grid-cols-3 gap-8">

                <div className="lg:col-span-2 bg-[var(--bg-secondary)] p-6 rounded-2xl shadow-2xl flex flex-col">
                    <div className="border-b border-[var(--border-color)] pb-4">
                        <div className="flex items-center justify-between mb-4">
                            <h1 className="text-xl font-semibold text-[var(--accent-color)]">{serverName}</h1>
                            <div className="flex items-center text-sm text-[var(--text-muted)]">
                                <Icon name="tower-broadcast" className="mr-2" />
                                <span>Connected</span>
                            </div>
                        </div>
                        <StreamSelector
                            streams={streams}
                            currentStreamId={currentStreamId}
                            onSelectStream={(streamId) => handleStreamChange(myClient.id, streamId)}
                            federationEnabled={settings.federation.enabled}
                            localServerId={getLocalServer()?.id}
                        />
                    </div>
                    {shouldShowControls ? (
                        <div className="flex-grow flex flex-col">
                            <div className="space-y-6">
                                <NowPlaying
                                    stream={currentStream}
                                    canSeek={streamCapabilities.canSeek}
                                    onSeek={handleSeek}
                                    onAlbumArtClick={toggleVisualizer}
                                />
                                <PlayerControls
                                    stream={currentStream}
                                    volume={myClient.volume}
                                    onVolumeChange={(vol) => handleVolumeChange(myClient.id, vol)}
                                    sourceVolume={currentStream.volume}
                                    onSourceVolumeChange={(vol) => handleSourceVolumeChange(currentStream.id, vol)}
                                    onPlayPause={handlePlayPause}
                                    onSkip={handleSkip}
                                />
                            </div>
                            <SyncedDevices
                                clients={syncedClients}
                                streams={streams}
                                onVolumeChange={handleVolumeChange}
                                onStreamChange={handleStreamChange}
                                onGroupVolumeAdjust={(dir) => handleGroupVolumeAdjust(myClient.currentStreamId, dir)}
                                onGroupMute={() => handleGroupMute(myClient.currentStreamId)}
                            />
                        </div>
                    ) : (
                        <div
                            className="flex-grow flex flex-col items-center justify-center bg-[var(--bg-secondary)] rounded-lg p-8 h-full min-h-[300px]">
                            <Icon name="music" className="text-6xl text-[var(--text-muted)] mb-4" />
                            <h2 className="text-2xl font-semibold text-[var(--text-primary)]">No Stream Selected</h2>
                            <p className="text-[var(--text-secondary)] mt-2">Choose a source to begin.</p>
                        </div>
                    )}
                </div>

                <div className="space-y-8">
                    <div className="bg-[var(--bg-secondary)] p-6 rounded-2xl shadow-2xl">
                        <h2 className="text-2xl font-bold text-[var(--accent-color)] border-b border-[var(--border-color)] pb-4 mb-4">Other
                            Streams &amp; Devices</h2>
                        <ClientManager
                            clients={otherClients}
                            streams={streams}
                            myClientStreamId={myClient.currentStreamId}
                            onVolumeChange={handleVolumeChange}
                            onStreamChange={handleStreamChange}
                            onGroupVolumeAdjust={handleGroupVolumeAdjust}
                            onGroupMute={handleGroupMute}
                            onStartBrowserAudio={() => {
                                const targetStream = myClient?.currentStreamId || null;
                                // Get the server for this stream (local or remote)
                                const server = targetStream ? getServerForStream(targetStream) : undefined;
                                // Fallback chain: server.host -> extract from stream ID -> window.location.hostname
                                const extractedHost = targetStream ? extractHostFromStreamId(targetStream) : undefined;
                                const targetHost = server?.host || extractedHost || window.location.hostname;
                                const targetPort = server?.port || 1780;

                                setTargetStreamForBrowserAudio(targetStream);
                                browserAudio.start(false, targetHost, targetPort);
                            }}
                            browserAudioActive={browserAudio.state.isActive}
                            federationEnabled={settings.federation.enabled}
                        />
                    </div>
                </div>
            </div>
            <footer
                className="w-full max-w-7xl mx-auto grid grid-cols-3 items-center text-[var(--text-muted)] mt-12 text-sm">
                <div>{/* Spacer */}</div>
                <p className="text-center">Sync Audio Controller &copy; 2024</p>
                <div className="flex justify-end gap-2">
                    <button
                        onClick={() => {
                            setSettingsInitialTab(undefined);
                            setIsSettingsOpen(true);
                        }}
                        className="p-2 rounded-full hover:bg-[var(--bg-secondary)] transition-colors"
                        aria-label="Open Settings"
                    >
                        <Icon name="gear" className="text-lg" />
                    </button>
                </div>
            </footer>
                    {isSettingsOpen && (
                        <SettingsModal
                            settings={settings}
                            onSettingsChange={(newSettings) => {
                                // Use settingsService to update settings (handles server + local storage)
                                settingsService.updateSettings(newSettings).catch((error) => {
                                    console.error('[Settings] Failed to update:', error);
                                });
                            }}
                            onClose={() => {
                                setIsSettingsOpen(false);
                                setSettingsInitialTab(undefined);
                            }}
                            initialTab={settingsInitialTab}
                        />
                    )}
                </div>
            } />
            </Routes>

            {/* Visualizer Overlay */}
            <Visualizer
                stream={currentStream}
                streams={streams}
                settings={settings}
                browserAudioSnapStream={browserAudio.getSnapStream?.() || null}
                browserAudioMuted={browserAudio.state.muted}
                extractedAlbumArtColors={extractedAlbumArtColors}
                onPlayPause={handlePlayPause}
                onSkip={handleSkip}
                onVolumeChange={(vol) => handleVolumeChange(myClient.id, vol)}
                onStreamChange={(streamId) => handleStreamChange(myClient.id, streamId)}
                onOpenSettings={() => {
                    setSettingsInitialTab(undefined);
                    setIsSettingsOpen(true);
                }}
                onOpenVisualizerSettings={() => {
                    setSettingsInitialTab('visualizer');
                    setIsSettingsOpen(true);
                }}
                onStartBrowserAudio={() => {
                    const targetStream = myClient?.currentStreamId || null;
                    // Get the server for this stream (local or remote)
                    const server = targetStream ? getServerForStream(targetStream) : undefined;
                    // Fallback chain: server.host -> extract from stream ID -> window.location.hostname
                    const extractedHost = targetStream ? extractHostFromStreamId(targetStream) : undefined;
                    const targetHost = server?.host || extractedHost || window.location.hostname;
                    const targetPort = server?.port || 1780;

                    setTargetStreamForBrowserAudio(targetStream);
                    browserAudio.start(true, targetHost, targetPort); // Start muted for visualizer mode
                }}
                onToggleBrowserAudioMute={() => browserAudio.toggleMute()}
                onClose={() => setIsVisualizerOpen(false)}
                currentVolume={myClient.volume}
                isOpen={isVisualizerOpen}
            />

            {/* Global Settings Modal */}
            {isSettingsOpen && (
                <SettingsModal
                    settings={settings}
                    onSettingsChange={(newSettings) => {
                        // Use settingsService to update settings (handles server + local storage)
                        settingsService.updateSettings(newSettings).catch((error) => {
                            console.error('[Settings] Failed to update:', error);
                        });
                    }}
                    onClose={() => {
                        setIsSettingsOpen(false);
                        setSettingsInitialTab(undefined);
                    }}
                    initialTab={settingsInitialTab}
                />
            )}
        </>
    );
};

export default App;