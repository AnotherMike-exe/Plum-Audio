import React, { useState } from 'react';
import type {AccentColor, Settings as SettingsType, ThemeMode} from '../../types';
import {Switch} from '../Switch';
import { Icon, type IconName } from '../Icon';
import { CustomColorPicker } from '../CustomColorPicker';

interface ThemeTabProps {
  settings: SettingsType;
  onSettingsChange: (newSettings: SettingsType) => void;
}

const themeModes: { value: ThemeMode; label: string; icon: IconName }[] = [
  {value: 'light', label: 'Light', icon: 'sun'},
  {value: 'dark', label: 'Dark', icon: 'moon'},
  {value: 'system', label: 'System', icon: 'desktop'},
  {value: 'black', label: 'Black', icon: 'moon'},
  {value: 'white', label: 'White', icon: 'sun'},
];

const accentColors: { name: AccentColor; className: string }[] = [
  {name: 'purple', className: 'bg-[#aa5cc3]'},
  {name: 'blue', className: 'bg-[#3b82f6]'},
  {name: 'green', className: 'bg-[#22c55e]'},
  {name: 'yellow', className: 'bg-[#eab308]'},
  {name: 'orange', className: 'bg-[#f97316]'},
  {name: 'red', className: 'bg-[#ef4444]'},
];

export const ThemeTab: React.FC<ThemeTabProps> = ({
  settings,
  onSettingsChange,
}) => {
  const [isCustomColorPickerOpen, setIsCustomColorPickerOpen] = useState(false);

  // Check if current mode is monochrome (disables theme mode toggle and accent colors)
  const isMonochromeMode = settings.theme.mode === 'black' || settings.theme.mode === 'white';

  const handleThemeModeChange = (mode: ThemeMode) => {
    onSettingsChange({
      ...settings,
      theme: {
        ...settings.theme,
        mode,
      },
    });
  };

  const handleAccentChange = (accent: AccentColor) => {
    onSettingsChange({
      ...settings,
      theme: {
        ...settings.theme,
        accent,
      },
    });
  };

  const handleCustomColorApply = (color: string) => {
    onSettingsChange({
      ...settings,
      theme: {
        ...settings.theme,
        accent: 'custom',
        customColor: color,
      },
    });
    setIsCustomColorPickerOpen(false);
  };

  const handleDisplayChange = (key: keyof SettingsType['display'], value: boolean) => {
    onSettingsChange({
      ...settings,
      display: {
        ...settings.display,
        [key]: value,
      },
    });
  };

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-base font-semibold text-[var(--text-primary)] mb-4">
          Appearance
        </h3>
        <p className="text-sm text-[var(--text-muted)] mb-6">
          Customize the look and feel of your interface
        </p>
      </div>

      <div className="space-y-6">
        <div>
          <h4 className="text-sm font-semibold text-[var(--text-primary)] mb-3">Theme Mode</h4>
          <div className="grid grid-cols-5 gap-2 p-1 rounded-lg bg-[var(--bg-tertiary)]">
            {themeModes.map((mode) => (
              <button
                key={mode.value}
                onClick={() => handleThemeModeChange(mode.value)}
                className={`
                  px-3 py-2 text-sm font-semibold rounded-md flex items-center justify-center gap-2 transition-colors
                  ${
                    settings.theme.mode === mode.value
                      ? 'bg-[var(--accent-color)] accent-button-text'
                      : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary-hover)]'
                  }
                `}
                title={mode.value === 'black' || mode.value === 'white' ? 'Monochrome mode - disables accent color selection' : ''}
              >
                <Icon name={mode.icon} className="text-lg" />
                {mode.label}
              </button>
            ))}
          </div>
          <p className="text-xs text-[var(--text-muted)] mt-2">
            {isMonochromeMode
              ? 'Monochrome modes use pure black or white themes with fixed accents'
              : 'System mode automatically switches between light and dark based on your device settings'
            }
          </p>
        </div>

        <div>
          <h4 className="text-sm font-semibold text-[var(--text-primary)] mb-3">
            Accent Color
            {isMonochromeMode && (
              <span className="ml-2 text-xs font-normal text-[var(--text-muted)]">(disabled in monochrome mode)</span>
            )}
          </h4>
          <div className="flex items-center gap-4">
            {accentColors.map((color) => (
              <button
                key={color.name}
                onClick={() => !isMonochromeMode && handleAccentChange(color.name)}
                disabled={isMonochromeMode}
                className={`
                  w-10 h-10 rounded-full ${color.className} transition-transform hover:scale-110
                  ${
                    settings.theme.accent === color.name && !isMonochromeMode
                      ? 'ring-2 ring-offset-2 ring-offset-[var(--bg-secondary)] ring-[var(--accent-color)]'
                      : ''
                  }
                  ${isMonochromeMode ? 'opacity-50 cursor-not-allowed' : ''}
                `}
                aria-label={`Set accent color to ${color.name}`}
                title={isMonochromeMode ? 'Not available in monochrome mode' : ''}
              />
            ))}

            {/* Custom Color Button */}
            <button
              onClick={() => !isMonochromeMode && setIsCustomColorPickerOpen(true)}
              disabled={isMonochromeMode}
              className={`
                w-10 h-10 rounded-full transition-transform hover:scale-110 flex items-center justify-center
                ${
                  settings.theme.accent === 'custom' && !isMonochromeMode
                    ? 'ring-2 ring-offset-2 ring-offset-[var(--bg-secondary)]'
                    : ''
                }
                ${isMonochromeMode ? 'opacity-50 cursor-not-allowed' : ''}
              `}
              style={{
                background: settings.theme.accent === 'custom' && settings.theme.customColor
                  ? `conic-gradient(from 0deg, red, yellow, lime, aqua, blue, magenta, red)`
                  : 'conic-gradient(from 0deg, red, yellow, lime, aqua, blue, magenta, red)',
                borderColor: settings.theme.accent === 'custom' && settings.theme.customColor
                  ? settings.theme.customColor
                  : 'transparent',
                borderWidth: settings.theme.accent === 'custom' && settings.theme.customColor ? '3px' : '0',
              }}
              aria-label="Set custom color"
              title={isMonochromeMode ? 'Not available in monochrome mode' : 'Choose custom color'}
            >
              {settings.theme.accent === 'custom' && settings.theme.customColor && (
                <div
                  className="w-6 h-6 rounded-full"
                  style={{ backgroundColor: settings.theme.customColor }}
                />
              )}
            </button>
          </div>
          <p className="text-xs text-[var(--text-muted)] mt-2">
            Choose a color that highlights active elements and buttons
          </p>
        </div>

        {/* Album Art Colors Toggle */}
        <div>
          <div className="flex items-center justify-between">
            <div>
              <h4 className="text-sm font-semibold text-[var(--text-primary)]">
                Album Art Colors
              </h4>
              <p className="text-xs text-[var(--text-muted)] mt-1">
                Extract background and accent colors from album artwork with guaranteed contrast
              </p>
            </div>
            <Switch
              checked={settings.theme.useAlbumArtColors || false}
              onChange={(checked) => {
                onSettingsChange({
                  ...settings,
                  theme: {
                    ...settings.theme,
                    useAlbumArtColors: checked,
                  },
                });
              }}
              label="Album Art Colors"
              disabled={isMonochromeMode}
            />
          </div>
          {settings.theme.useAlbumArtColors && !isMonochromeMode && (
            <p className="text-xs text-[var(--text-muted)] mt-2 italic">
              When enabled, UI colors will dynamically match your currently playing artwork.
              Colors are automatically adjusted to meet WCAG accessibility standards.
              Falls back to selected theme when no artwork is available.
            </p>
          )}
          {isMonochromeMode && (
            <p className="text-xs text-[var(--text-muted)] mt-2 italic opacity-70">
              Not available in monochrome mode.
            </p>
          )}
        </div>

      </div>

      <div className="pt-4 mt-6 border-t border-[var(--border-color)]">
        <p className="text-xs text-[var(--text-muted)] italic">
          Note: Theme and display preferences are saved per-browser. Different devices can have
          different theme settings while sharing the same server configuration.
        </p>
      </div>

      {/* Custom Color Picker Modal */}
      {isCustomColorPickerOpen && (
        <CustomColorPicker
          initialColor={settings.theme.customColor || '#aa5cc3'}
          onApply={handleCustomColorApply}
          onCancel={() => setIsCustomColorPickerOpen(false)}
        />
      )}
    </div>
  );
};
