import React, { useState, useEffect } from 'react';
import { Icon } from '../Icon';
import type { Settings as SettingsType } from '../../types';
import { deviceSettingsService } from '../../services/deviceSettingsService';
import { settingsService } from '../../services/settingsService';

interface AboutTabProps {
  settings: SettingsType;
  onSettingsChange: (newSettings: SettingsType) => void;
}

export const AboutTab: React.FC<AboutTabProps> = ({ settings, onSettingsChange }) => {
  const [deviceName, setDeviceName] = useState('');
  const [hostname, setHostname] = useState('');
  const [hostnameError, setHostnameError] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState('');

  // Initialize form from settings
  useEffect(() => {
    setDeviceName(settings.deviceName || 'Plum Snapcast');
    setHostname(settings.hostname || 'plum-snapcast');
  }, [settings.deviceName, settings.hostname]);

  // Check if settings have changed
  const hasChanges =
    deviceName !== (settings.deviceName || 'Plum Snapcast') ||
    hostname !== (settings.hostname || 'plum-snapcast');

  // Validate hostname on change
  const handleHostnameChange = async (value: string) => {
    setHostname(value);
    setHostnameError('');

    if (!value.trim()) {
      setHostnameError('Hostname cannot be empty');
      return;
    }

    // Only validate if it looks like a complete hostname
    if (value.length >= 3) {
      const result = await deviceSettingsService.validateHostname(value);
      if (!result.valid && result.error) {
        setHostnameError(result.error);
      }
    }
  };

  // Update device name (hostname is independent)
  const handleDeviceNameChange = (value: string) => {
    setDeviceName(value);
  };

  // Save device settings
  const handleSave = async () => {
    if (hostnameError) {
      return;
    }

    setSaving(true);
    setSaveMessage('');

    try {
      // Only send fields that have changed to avoid unnecessary Avahi restarts
      const updates: { deviceName?: string; hostname?: string } = {};

      if (deviceName.trim() !== (settings.deviceName || 'Plum Snapcast')) {
        updates.deviceName = deviceName.trim();
      }

      if (hostname.trim() !== (settings.hostname || 'plum-snapcast')) {
        updates.hostname = hostname.trim();
      }

      const response = await deviceSettingsService.updateDevice(updates);

      setSaveMessage(response.message || 'Settings saved successfully');

      // Refresh settings from server
      await settingsService.refresh();

      // Clear message after 3 seconds
      setTimeout(() => setSaveMessage(''), 3000);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to save settings';
      setSaveMessage(message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Device Settings Section */}
      <div>
        <h3 className="text-base font-semibold text-[var(--text-primary)] mb-4">
          Device Settings
        </h3>

        <div className="space-y-4">
          <div className="p-4 bg-[var(--bg-tertiary)] rounded-lg border border-[var(--border-color)]">
            <div className="space-y-4">
              {/* Device Name */}
              <div>
                <label className="block text-sm font-medium text-[var(--text-primary)] mb-2">
                  Device Name
                </label>
                <input
                  type="text"
                  value={deviceName}
                  onChange={(e) => handleDeviceNameChange(e.target.value)}
                  disabled={saving}
                  className="w-full px-3 py-2 bg-[var(--bg-secondary)] border border-[var(--border-color)] rounded-md text-[var(--text-primary)] text-sm focus:outline-none focus:ring-2 focus:ring-[var(--accent-color)] disabled:opacity-50"
                  placeholder="Plum Snapcast"
                />
                <p className="mt-1 text-xs text-[var(--text-muted)]">
                  Display name used in Federation and the browser title
                </p>
              </div>

              {/* Hostname */}
              <div>
                <label className="block text-sm font-medium text-[var(--text-primary)] mb-2">
                  Hostname
                </label>
                <input
                  type="text"
                  value={hostname}
                  onChange={(e) => handleHostnameChange(e.target.value)}
                  disabled={saving}
                  className={`w-full px-3 py-2 bg-[var(--bg-secondary)] border rounded-md text-[var(--text-primary)] text-sm focus:outline-none focus:ring-2 focus:ring-[var(--accent-color)] disabled:opacity-50 ${
                    hostnameError ? 'border-red-500' : 'border-[var(--border-color)]'
                  }`}
                  placeholder="plum-snapcast"
                />
                {hostnameError ? (
                  <p className="mt-1 text-xs text-red-500">{hostnameError}</p>
                ) : (
                  <p className="mt-1 text-xs text-[var(--text-muted)]">
                    Access this device at http://{hostname || 'plum-snapcast'}.local (mDNS/Avahi). Lowercase letters, numbers, and hyphens only.
                  </p>
                )}
              </div>

              {/* Save Button */}
              <div className="flex items-center gap-3 pt-2">
                <button
                  onClick={handleSave}
                  disabled={!hasChanges || saving || !!hostnameError}
                  className="px-4 py-2 bg-[var(--accent-color)] accent-button-text rounded-md text-sm font-medium hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed transition-opacity"
                >
                  {saving ? 'Saving...' : 'Save Changes'}
                </button>
                {saveMessage && (
                  <span className={`text-sm ${saveMessage.toLowerCase().includes('error') || saveMessage.toLowerCase().includes('failed') ? 'text-red-500' : 'text-green-500'}`}>
                    {saveMessage}
                  </span>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* About Section */}
      <div>
        <h3 className="text-base font-semibold text-[var(--text-primary)] mb-4">
          About Plum-Snapcast
        </h3>
        <p className="text-sm text-[var(--text-muted)] mb-6">
          Multi-room audio streaming with Snapcast
        </p>
      </div>

      <div className="space-y-6">
        <div className="p-4 bg-[var(--bg-tertiary)] rounded-lg border border-[var(--border-color)]">
          <h4 className="text-sm font-semibold text-[var(--text-primary)] mb-3">
            Version Information
          </h4>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between">
              <span className="text-[var(--text-muted)]">Plum-Snapcast</span>
              <span className="text-[var(--text-primary)] font-mono">1.0.0</span>
            </div>
            <div className="flex justify-between">
              <span className="text-[var(--text-muted)]">Frontend</span>
              <span className="text-[var(--text-primary)] font-mono">React 19.1.1</span>
            </div>
          </div>
        </div>

        <div className="p-4 bg-[var(--bg-tertiary)] rounded-lg border border-[var(--border-color)]">
          <h4 className="text-sm font-semibold text-[var(--text-primary)] mb-3">
            Credits & Attribution
          </h4>
          <div className="space-y-3 text-sm text-[var(--text-muted)]">
            <div>
              <p className="font-semibold text-[var(--text-primary)] mb-1">Based on</p>
              <ul className="space-y-1 list-disc list-inside">
                <li>
                  <a
                    href="https://github.com/badaix/snapcast"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-[var(--accent-color)] hover:underline"
                  >
                    Snapcast
                  </a>
                  {' '}by Johannes Pohl
                </li>
                <li>
                  <a
                    href="https://github.com/firefrei/docker-snapcast"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-[var(--accent-color)] hover:underline"
                  >
                    docker-snapcast
                  </a>
                  {' '}by firefrei
                </li>
              </ul>
            </div>

            <div>
              <p className="font-semibold text-[var(--text-primary)] mb-1">Audio Sources</p>
              <ul className="space-y-1 list-disc list-inside">
                <li>
                  <a
                    href="https://github.com/mikebrady/shairport-sync"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-[var(--accent-color)] hover:underline"
                  >
                    Shairport-Sync
                  </a>
                  {' '}(AirPlay)
                </li>
                <li>
                  <a
                    href="https://github.com/Spotifyd/spotifyd"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-[var(--accent-color)] hover:underline"
                  >
                    Spotifyd
                  </a>
                  {' '}(Spotify Connect)
                </li>
                <li>
                  <a
                    href="https://github.com/hzeller/gmrender-resurrect"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-[var(--accent-color)] hover:underline"
                  >
                    gmrender-resurrect
                  </a>
                  {' '}(DLNA/UPnP)
                </li>
              </ul>
            </div>
          </div>
        </div>

        <div className="p-4 bg-[var(--bg-tertiary)] rounded-lg border border-[var(--border-color)]">
          <h4 className="text-sm font-semibold text-[var(--text-primary)] mb-3">
            Resources
          </h4>
          <div className="space-y-2 text-sm">
            <a
              href="https://github.com/Mprice12337/Plum-Snapcast"
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-2 text-[var(--accent-color)] hover:underline"
            >
              <Icon name="github" className="text-lg" style={{ color: 'inherit' }} />
              <span>View on GitHub</span>
            </a>
            <a
              href="https://github.com/badaix/snapcast/blob/develop/doc/json_rpc_api/"
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-2 text-[var(--accent-color)] hover:underline"
            >
              <Icon name="book" className="text-lg" style={{ color: 'inherit' }} />
              <span>Snapcast API Documentation</span>
            </a>
          </div>
        </div>

        <div className="text-center pt-4">
          <p className="text-xs text-[var(--text-muted)]">
            Built with Claude Code
          </p>
        </div>
      </div>
    </div>
  );
};
