/**
 * Settings Service
 * Manages two-tier settings architecture:
 * - Server settings (global): integrations, federation
 * - Browser-local settings (per-device): theme, display
 */

import type {Settings, ThemeMode, AccentColor} from '../types';
import {DEFAULT_VISUALIZER_SETTINGS} from '../types';

const SETTINGS_API_URL = '/api/settings';
const LOCAL_STORAGE_KEY = 'plum-snapcast-local-settings';

// Default local settings (browser-specific)
const DEFAULT_LOCAL_SETTINGS = {
  theme: {
    mode: 'system' as ThemeMode,
    accent: 'purple' as AccentColor,
    useAlbumArtColors: false,
  },
  display: {
    showOfflineDevices: false,
  },
};

// Default server settings (should match backend defaults)
const DEFAULT_SERVER_SETTINGS = {
  version: 1,  // Version for change detection
  deviceName: 'Plum Snapcast',
  hostname: 'plum-snapcast',
  integrations: {
    airplay: {
      enabled: true,
      deviceName: 'Plum Audio',
    },
    bluetooth: {
      enabled: false,
      deviceName: 'Plum Audio',
      adapter: 'hci0',
      autoPair: true,
      discoverable: true,
    },
    spotify: {
      enabled: false,
      sourceName: 'Spotify',
      deviceName: 'Plum Audio',
      bitrate: 320 as 96 | 160 | 320,
    },
    dlna: {
      enabled: false,
      sourceName: 'DLNA',
      deviceName: 'Plum Audio',
    },
    plexamp: {
      available: false,
      enabled: false,
      sourceName: 'Plexamp',
    },
    snapcast: true,
    visualizer: DEFAULT_VISUALIZER_SETTINGS,
  },
  federation: {
    enabled: false,
    autoDiscover: true,
  },
  autoSwitch: {
    localActivity: true,
    slave: {
      enabled: false,
      masterHost: '',
      masterWsPort: 1780,
      masterStreamPort: 1704,
    },
  },
};

interface ServerSettings {
  deviceName: Settings['deviceName'];
  hostname: Settings['hostname'];
  integrations: Settings['integrations'];
  federation: Settings['federation'];
  autoSwitch?: Settings['autoSwitch'];
  snapclientTarget?: string;
}

interface LocalSettings {
  theme: Settings['theme'];
  display: Settings['display'];
}

class SettingsService {
  private serverSettings: ServerSettings & { version?: number } = DEFAULT_SERVER_SETTINGS;
  private localSettings: LocalSettings = DEFAULT_LOCAL_SETTINGS;
  private listeners: Array<(settings: Settings) => void> = [];
  private pollingInterval: number | null = null;
  private readonly POLL_INTERVAL_MS = 10000; // Poll every 10 seconds

  constructor() {
    this.loadLocalSettings();
  }

  /**
   * Initialize settings service by loading from server and local storage
   */
  async init(): Promise<Settings> {
    try {
      await this.fetchServerSettings();
    } catch (error) {
      console.error('Failed to fetch server settings, using defaults:', error);
    }

    // Start polling for settings changes
    this.startPolling();

    return this.getMergedSettings();
  }

  /**
   * Start polling for settings changes
   */
  private startPolling(): void {
    if (this.pollingInterval !== null) {
      return; // Already polling
    }

    this.pollingInterval = window.setInterval(async () => {
      try {
        await this.checkForUpdates();
      } catch (error) {
        // Silently handle errors to avoid spam
        console.debug('Settings poll failed:', error);
      }
    }, this.POLL_INTERVAL_MS);

    console.log('[Settings] Started polling for changes');
  }

  /**
   * Stop polling for settings changes
   */
  private stopPolling(): void {
    if (this.pollingInterval !== null) {
      window.clearInterval(this.pollingInterval);
      this.pollingInterval = null;
      console.log('[Settings] Stopped polling');
    }
  }

  /**
   * Check for settings updates and refresh if version changed
   */
  private async checkForUpdates(): Promise<void> {
    const response = await fetch(SETTINGS_API_URL);
    if (!response.ok) {
      return;
    }

    const data = await response.json();
    const serverVersion = data.version || 0;
    const currentVersion = this.serverSettings.version || 0;
    const newTarget = data.snapclientTarget as string | undefined;
    const currentTarget = this.serverSettings.snapclientTarget;

    if (serverVersion > currentVersion) {
      console.log(`[Settings] Version changed: ${currentVersion} → ${serverVersion}`);
      await this.fetchServerSettings();
      const merged = this.getMergedSettings();
      this.notifyListeners(merged);
    } else if (newTarget !== currentTarget) {
      // snapclientTarget changed without a version bump (auto-switch service wrote the file)
      console.log(`[Settings] Snapclient target changed: ${currentTarget} → ${newTarget}`);
      this.serverSettings = { ...this.serverSettings, snapclientTarget: newTarget };
      const merged = this.getMergedSettings();
      this.notifyListeners(merged);
    }
  }

  /**
   * Get merged settings (server + local overrides)
   */
  getMergedSettings(): Settings {
    return {
      ...this.serverSettings,
      ...this.localSettings,
    };
  }

  /**
   * Fetch settings from server
   */
  private async fetchServerSettings(): Promise<void> {
    const response = await fetch(SETTINGS_API_URL);
    if (!response.ok) {
      throw new Error(`Failed to fetch settings: ${response.statusText}`);
    }
    const data = await response.json();
    this.serverSettings = {
      version: data.version || 1,
      deviceName: data.deviceName || DEFAULT_SERVER_SETTINGS.deviceName,
      hostname: data.hostname || DEFAULT_SERVER_SETTINGS.hostname,
      integrations: data.integrations || DEFAULT_SERVER_SETTINGS.integrations,
      federation: data.federation || DEFAULT_SERVER_SETTINGS.federation,
      autoSwitch: data.autoSwitch ?? DEFAULT_SERVER_SETTINGS.autoSwitch,
      snapclientTarget: data.snapclientTarget,
    };
  }

  /**
   * Load local settings from localStorage
   */
  private loadLocalSettings(): void {
    try {
      const stored = localStorage.getItem(LOCAL_STORAGE_KEY);
      if (stored) {
        const parsed = JSON.parse(stored);
        this.localSettings = {
          theme: {
            ...DEFAULT_LOCAL_SETTINGS.theme,
            ...parsed.theme,
          },
          display: {
            ...DEFAULT_LOCAL_SETTINGS.display,
            ...parsed.display,
          },
        };
      }
    } catch (error) {
      console.error('Failed to load local settings:', error);
      this.localSettings = DEFAULT_LOCAL_SETTINGS;
    }
  }

  /**
   * Save local settings to localStorage
   */
  private saveLocalSettings(): void {
    try {
      localStorage.setItem(LOCAL_STORAGE_KEY, JSON.stringify(this.localSettings));
    } catch (error) {
      console.error('Failed to save local settings:', error);
    }
  }

  /**
   * Update server settings (integrations, federation)
   */
  async updateServerSettings(
    updates: Partial<ServerSettings>
  ): Promise<Settings> {
    try {
      const response = await fetch(SETTINGS_API_URL, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(updates),
      });

      if (!response.ok) {
        throw new Error(`Failed to update settings: ${response.statusText}`);
      }

      const data = await response.json();
      this.serverSettings = {
        version: data.version || this.serverSettings.version,
        deviceName: data.deviceName || this.serverSettings.deviceName,
        hostname: data.hostname || this.serverSettings.hostname,
        integrations: data.integrations || this.serverSettings.integrations,
        federation: data.federation || this.serverSettings.federation,
        autoSwitch: data.autoSwitch ?? this.serverSettings.autoSwitch,
        snapclientTarget: data.snapclientTarget ?? this.serverSettings.snapclientTarget,
      };

      const merged = this.getMergedSettings();
      this.notifyListeners(merged);
      return merged;
    } catch (error) {
      console.error('Failed to update server settings:', error);
      throw error;
    }
  }

  /**
   * Update local settings (theme, display)
   */
  updateLocalSettings(updates: Partial<LocalSettings>): Settings {
    if (updates.theme) {
      this.localSettings.theme = {
        ...this.localSettings.theme,
        ...updates.theme,
      };
    }
    if (updates.display) {
      this.localSettings.display = {
        ...this.localSettings.display,
        ...updates.display,
      };
    }

    this.saveLocalSettings();
    const merged = this.getMergedSettings();
    this.notifyListeners(merged);
    return merged;
  }

  /**
   * Update full settings (determines which tier to update)
   */
  async updateSettings(updates: Partial<Settings>): Promise<Settings> {
    // Split updates into server and local
    const serverUpdates: Partial<ServerSettings> = {};
    const localUpdates: Partial<LocalSettings> = {};

    if (updates.deviceName !== undefined) {
      serverUpdates.deviceName = updates.deviceName;
    }
    if (updates.hostname !== undefined) {
      serverUpdates.hostname = updates.hostname;
    }
    if (updates.integrations) {
      serverUpdates.integrations = updates.integrations;
    }
    if (updates.federation) {
      serverUpdates.federation = updates.federation;
    }
    if (updates.autoSwitch !== undefined) {
      serverUpdates.autoSwitch = updates.autoSwitch;
    }
    if (updates.theme) {
      localUpdates.theme = updates.theme;
    }
    if (updates.display) {
      localUpdates.display = updates.display;
    }

    // Update server settings if needed
    if (Object.keys(serverUpdates).length > 0) {
      await this.updateServerSettings(serverUpdates);
    }

    // Update local settings if needed
    if (Object.keys(localUpdates).length > 0) {
      this.updateLocalSettings(localUpdates);
    }

    return this.getMergedSettings();
  }

  /**
   * Subscribe to settings changes
   */
  subscribe(listener: (settings: Settings) => void): () => void {
    this.listeners.push(listener);
    return () => {
      this.listeners = this.listeners.filter((l) => l !== listener);
    };
  }

  /**
   * Notify all listeners of settings changes
   */
  private notifyListeners(settings: Settings): void {
    this.listeners.forEach((listener) => listener(settings));
  }

  /**
   * Refresh settings from server
   */
  async refresh(): Promise<Settings> {
    await this.fetchServerSettings();
    const merged = this.getMergedSettings();
    this.notifyListeners(merged);
    return merged;
  }
}

// Export singleton instance
export const settingsService = new SettingsService();
