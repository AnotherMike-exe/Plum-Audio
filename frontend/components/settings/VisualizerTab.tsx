import React, { useState } from 'react';
import type {Settings as SettingsType, VisualizerSettings, VisualizerPreset} from '../../types';
import {DEFAULT_VISUALIZER_SETTINGS, BUILT_IN_PRESETS} from '../../types';
import {Switch} from '../Switch';

interface VisualizerTabProps {
  settings: SettingsType;
  onSettingsChange: (newSettings: SettingsType) => void;
}

export const VisualizerTab: React.FC<VisualizerTabProps> = ({
  settings,
  onSettingsChange,
}) => {
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [presetName, setPresetName] = useState('');

  // Helper to get visualizer settings (supports legacy boolean)
  const getVisualizerSettings = (): VisualizerSettings => {
    const viz = settings.integrations.visualizer;
    if (typeof viz === 'boolean') {
      return { ...DEFAULT_VISUALIZER_SETTINGS, enabled: viz };
    }
    // Merge with defaults to ensure all fields have values
    return { ...DEFAULT_VISUALIZER_SETTINGS, ...viz };
  };

  // Update individual visualizer settings
  const handleVisualizerChange = (key: keyof VisualizerSettings, value: any) => {
    const current = getVisualizerSettings();
    onSettingsChange({
      ...settings,
      integrations: {
        ...settings.integrations,
        visualizer: { ...current, [key]: value }
      }
    });
  };

  // Update advanced settings
  const handleAdvancedChange = (key: keyof VisualizerSettings['advanced'], value: any) => {
    const current = getVisualizerSettings();
    onSettingsChange({
      ...settings,
      integrations: {
        ...settings.integrations,
        visualizer: {
          ...current,
          advanced: { ...current.advanced, [key]: value }
        }
      }
    });
  };

  // Preset management
  const viz = getVisualizerSettings();
  const userPresets = settings.integrations.visualizerPresets || [];
  const allPresets = [...BUILT_IN_PRESETS, ...userPresets];

  const handleSavePreset = () => {
    if (!presetName.trim()) return;

    const newPreset: VisualizerPreset = {
      id: Date.now().toString(),
      name: presetName.trim(),
      settings: {
        theme: viz.theme,
        type: viz.type,
        barCount: viz.barCount,
        sensitivity: viz.sensitivity,
        smoothing: viz.smoothing,
        smoothingType: viz.smoothingType,
        frequencyScale: viz.frequencyScale,
        idleState: viz.idleState,
        symmetry: viz.symmetry,
        mirror: viz.mirror,
        invert: viz.invert,
        taper: viz.taper,
        rotate: viz.rotate,
        rotationSpeed: viz.rotationSpeed,
        rotationDirection: viz.rotationDirection,
        advanced: viz.advanced,
      }
    };

    onSettingsChange({
      ...settings,
      integrations: {
        ...settings.integrations,
        visualizerPresets: [...userPresets, newPreset]
      }
    });
    setPresetName('');
  };

  const handleLoadPreset = (presetId: string) => {
    const preset = allPresets.find(p => p.id === presetId);
    if (!preset) return;

    onSettingsChange({
      ...settings,
      integrations: {
        ...settings.integrations,
        visualizer: {
          enabled: viz.enabled,
          cycleEnabled: viz.cycleEnabled,
          cyclePresetIds: viz.cyclePresetIds,
          ...preset.settings
        }
      }
    });
  };

  const handleDeletePreset = (presetId: string) => {
    // Prevent deletion of built-in presets
    const preset = allPresets.find(p => p.id === presetId);
    if (preset?.isBuiltIn) return;

    onSettingsChange({
      ...settings,
      integrations: {
        ...settings.integrations,
        visualizerPresets: userPresets.filter(p => p.id !== presetId)
      }
    });
  };

  const handleToggleCyclePreset = (presetId: string) => {
    const currentIds = viz.cyclePresetIds || [];
    const newIds = currentIds.includes(presetId)
      ? currentIds.filter(id => id !== presetId)
      : [...currentIds, presetId];

    handleVisualizerChange('cyclePresetIds', newIds);
  };

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-base font-semibold text-[var(--text-primary)] mb-4">
          Audio Visualizer
        </h3>
        <p className="text-sm text-[var(--text-muted)] mb-4">
          Real-time audio visualization with customizable effects and presets
        </p>
      </div>

      <div className="space-y-4">
        <div className="space-y-6">
            {/* Preset Cycling */}
            <div className="pb-4 border-b border-[var(--border-color)]">
              <h4 className="text-sm font-semibold text-[var(--text-primary)] mb-3">
                Preset Cycling
              </h4>
              <p className="text-xs text-[var(--text-muted)] mb-3">
                Automatically cycle through selected presets on track changes
              </p>

              <Switch
                label="Enable Preset Cycling"
                checked={viz.cycleEnabled}
                onChange={(val) => handleVisualizerChange('cycleEnabled', val)}
                icon="arrows-rotate"
              />

              {viz.cycleEnabled && (
                <div className="mt-4 pl-8">
                  <label className="text-xs text-[var(--text-muted)] mb-2 block">
                    Select Presets to Cycle ({viz.cyclePresetIds.length} selected)
                  </label>
                  <div className="space-y-2">
                    {allPresets.map(preset => (
                      <label
                        key={preset.id}
                        className="flex items-center gap-2 px-3 py-2 rounded-md bg-[var(--bg-tertiary)] hover:bg-[var(--bg-tertiary-hover)] cursor-pointer transition-colors"
                      >
                        <input
                          type="checkbox"
                          checked={viz.cyclePresetIds.includes(preset.id)}
                          onChange={() => handleToggleCyclePreset(preset.id)}
                          className="w-4 h-4 rounded border-[var(--border-color)] text-[var(--accent-color)] focus:ring-[var(--accent-color)]"
                        />
                        <span className="flex-1 text-sm text-[var(--text-primary)]">
                          {preset.name}
                          {preset.isBuiltIn && (
                            <span className="ml-2 text-xs text-[var(--text-muted)]">(Built-in)</span>
                          )}
                        </span>
                      </label>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {/* Preset Management */}
            <div className="pb-4 border-b border-[var(--border-color)]">
              <h4 className="text-sm font-semibold text-[var(--text-primary)] mb-3">
                Presets
              </h4>

              {/* Built-in Presets */}
              <div className="mb-4">
                <label className="text-xs text-[var(--text-muted)] mb-2 block">
                  Built-in Presets
                </label>
                <div className="grid grid-cols-2 gap-2">
                  {BUILT_IN_PRESETS.map(preset => (
                    <button
                      key={preset.id}
                      onClick={() => handleLoadPreset(preset.id)}
                      className="px-3 py-2 text-sm font-semibold rounded-md bg-[var(--bg-tertiary)] text-[var(--text-primary)] hover:bg-[var(--accent-color)] hover:accent-button-text transition-colors"
                    >
                      {preset.name}
                    </button>
                  ))}
                </div>
              </div>

              {/* User Presets */}
              {userPresets.length > 0 && (
                <div className="mb-4">
                  <label className="text-xs text-[var(--text-muted)] mb-2 block">
                    Your Presets
                  </label>
                  <div className="grid grid-cols-2 gap-2">
                    {userPresets.map(preset => (
                      <div key={preset.id} className="flex items-center gap-2">
                        <button
                          onClick={() => handleLoadPreset(preset.id)}
                          className="flex-1 px-3 py-2 text-sm font-semibold rounded-md bg-[var(--bg-tertiary)] text-[var(--text-primary)] hover:bg-[var(--accent-color)] hover:accent-button-text transition-colors"
                        >
                          {preset.name}
                        </button>
                        <button
                          onClick={() => handleDeletePreset(preset.id)}
                          className="px-2 py-2 text-sm rounded-md bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:text-red-500 transition-colors"
                          title="Delete preset"
                        >
                          ✕
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Save Preset */}
              <div>
                <label className="text-xs text-[var(--text-muted)] mb-2 block">
                  Save Current Settings as Preset
                </label>
                <div className="flex items-center gap-2">
                  <input
                    type="text"
                    value={presetName}
                    onChange={(e) => setPresetName(e.target.value)}
                    placeholder="Preset name..."
                    className="flex-1 px-3 py-2 text-sm bg-[var(--bg-tertiary)] text-[var(--text-primary)] rounded-md border border-[var(--border-color)] focus:outline-none focus:border-[var(--accent-color)]"
                    onKeyDown={(e) => {
                      // Prevent spacebar and other keys from bubbling up to parent handlers (e.g., play/pause)
                      if (e.key === ' ' || e.key === 'ArrowUp' || e.key === 'ArrowDown') {
                        e.stopPropagation();
                      }
                      // Handle Enter to save
                      if (e.key === 'Enter') {
                        handleSavePreset();
                      }
                    }}
                  />
                  <button
                    onClick={handleSavePreset}
                    disabled={!presetName.trim()}
                    className="px-4 py-2 text-sm font-semibold rounded-md bg-[var(--accent-color)] accent-button-text hover:brightness-110 transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    Save
                  </button>
                </div>
              </div>
            </div>

            {/* Waveform Type */}
            <div>
              <h4 className="text-sm font-semibold text-[var(--text-primary)] mb-3">
                Waveform Type
              </h4>
              <div className="grid grid-cols-5 gap-2 p-1 rounded-lg bg-[var(--bg-tertiary)]">
                {[
                  { value: 'bars', label: 'Bars' },
                  { value: 'circular', label: 'Circular' },
                  { value: 'circular-bars', label: 'Radial' },
                  { value: 'waveform', label: 'Wave' },
                  { value: 'mixed', label: 'Mixed' }
                ].map(type => (
                  <button
                    key={type.value}
                    onClick={() => handleVisualizerChange('type', type.value)}
                    className={`px-3 py-2 text-sm font-semibold rounded-md transition-colors ${
                      viz.type === type.value
                        ? 'bg-[var(--accent-color)] accent-button-text'
                        : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary-hover)]'
                    }`}
                  >
                    {type.label}
                  </button>
                ))}
              </div>
            </div>

            {/* Bar Count */}
            <div>
              <label className="text-sm font-semibold text-[var(--text-primary)] mb-2 block">
                Bar Count: {viz.barCount}
              </label>
              <div className="grid grid-cols-4 gap-2">
                {[32, 64, 128, 256].map(count => (
                  <button
                    key={count}
                    onClick={() => handleVisualizerChange('barCount', count)}
                    className={`px-3 py-2 text-sm font-semibold rounded-md transition-colors ${
                      viz.barCount === count
                        ? 'bg-[var(--accent-color)] accent-button-text'
                        : 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary-hover)]'
                    }`}
                  >
                    {count}
                  </button>
                ))}
              </div>
              {viz.barCount > 128 && (
                <p className="text-xs text-yellow-400 mt-2">
                  ⚠ High bar counts may impact performance on Raspberry Pi
                </p>
              )}
            </div>

            {/* Sensitivity Slider */}
            <div>
              <label className="text-sm font-semibold text-[var(--text-primary)] mb-2 block">
                Sensitivity: {viz.sensitivity}
              </label>
              <input
                type="range"
                min="0"
                max="100"
                value={viz.sensitivity}
                onChange={(e) => handleVisualizerChange('sensitivity', parseInt(e.target.value))}
                className="volume-slider w-full"
              />
              <p className="text-xs text-[var(--text-muted)] mt-1">
                Controls how responsive the visualizer is to audio levels
              </p>
            </div>

            {/* Smoothing Slider */}
            <div>
              <label className="text-sm font-semibold text-[var(--text-primary)] mb-2 block">
                Smoothing: {viz.smoothing}
              </label>
              <input
                type="range"
                min="0"
                max="100"
                value={viz.smoothing}
                onChange={(e) => handleVisualizerChange('smoothing', parseInt(e.target.value))}
                className="volume-slider w-full"
              />
              <p className="text-xs text-[var(--text-muted)] mt-1">
                FFT smoothing - higher values create smoother frequency analysis
              </p>
            </div>

            {/* Frequency Scale */}
            <div>
              <h4 className="text-sm font-semibold text-[var(--text-primary)] mb-3">
                Frequency Scale
              </h4>
              <div className="grid grid-cols-3 gap-2 p-1 rounded-lg bg-[var(--bg-tertiary)]">
                {[
                  { value: 'linear', label: 'Linear', description: 'Equal frequency spacing' },
                  { value: 'logarithmic', label: 'Logarithmic', description: 'Natural hearing response' },
                  { value: 'logarithmic-smooth', label: 'Log + Smooth', description: 'Smoothed blob shape' }
                ].map(scale => (
                  <button
                    key={scale.value}
                    onClick={() => handleVisualizerChange('frequencyScale', scale.value)}
                    className={`px-3 py-2 text-sm font-semibold rounded-md transition-colors ${
                      viz.frequencyScale === scale.value
                        ? 'bg-[var(--accent-color)] accent-button-text'
                        : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary-hover)]'
                    }`}
                    title={scale.description}
                  >
                    {scale.label}
                  </button>
                ))}
              </div>
              <p className="text-xs text-[var(--text-muted)] mt-2">
                Controls frequency distribution: linear (equal spacing), logarithmic (natural), or smoothed blob
              </p>
            </div>

            {/* Smoothing Type */}
            <div>
              <h4 className="text-sm font-semibold text-[var(--text-primary)] mb-3">
                Curve Smoothing
              </h4>
              <div className="grid grid-cols-3 gap-2 p-1 rounded-lg bg-[var(--bg-tertiary)]">
                {[
                  { value: 'catmull-rom', label: 'Catmull-Rom', description: 'Smooth curves through points' },
                  { value: 'bezier', label: 'Bezier', description: 'Very smooth, organic' },
                  { value: 'simple', label: 'Simple', description: 'Basic interpolation' }
                ].map(type => (
                  <button
                    key={type.value}
                    onClick={() => handleVisualizerChange('smoothingType', type.value)}
                    className={`px-3 py-2 text-sm font-semibold rounded-md transition-colors ${
                      viz.smoothingType === type.value
                        ? 'bg-[var(--accent-color)] accent-button-text'
                        : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary-hover)]'
                    }`}
                    title={type.description}
                  >
                    {type.label}
                  </button>
                ))}
              </div>
            </div>

            {/* Idle State */}
            <div>
              <h4 className="text-sm font-semibold text-[var(--text-primary)] mb-3">
                Idle State (No Audio)
              </h4>
              <div className="grid grid-cols-3 gap-2 p-1 rounded-lg bg-[var(--bg-tertiary)]">
                {[
                  { value: 'circle', label: 'Circle', description: 'Perfect circle outline' },
                  { value: 'pulse', label: 'Pulse', description: 'Gentle pulsing circle' },
                  { value: 'nothing', label: 'Nothing', description: 'Blank when idle' }
                ].map(state => (
                  <button
                    key={state.value}
                    onClick={() => handleVisualizerChange('idleState', state.value)}
                    className={`px-3 py-2 text-sm font-semibold rounded-md transition-colors ${
                      viz.idleState === state.value
                        ? 'bg-[var(--accent-color)] accent-button-text'
                        : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary-hover)]'
                    }`}
                    title={state.description}
                  >
                    {state.label}
                  </button>
                ))}
              </div>
            </div>

            {/* Symmetry */}
            <div>
              <h4 className="text-sm font-semibold text-[var(--text-primary)] mb-3">
                Symmetry
              </h4>
              <div className="grid grid-cols-4 gap-2 p-1 rounded-lg bg-[var(--bg-tertiary)]">
                {[
                  { value: 1, label: '1x', description: 'Full spectrum (all unique)' },
                  { value: 2, label: '2x', description: 'Mirror pattern twice' },
                  { value: 3, label: '3x', description: 'Repeat pattern 3 times' },
                  { value: 4, label: '4x', description: 'Repeat pattern 4 times' }
                ].map(sym => (
                  <button
                    key={sym.value}
                    onClick={() => handleVisualizerChange('symmetry', sym.value)}
                    className={`px-3 py-2 text-sm font-semibold rounded-md transition-colors ${
                      viz.symmetry === sym.value
                        ? 'bg-[var(--accent-color)] accent-button-text'
                        : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary-hover)]'
                    }`}
                    title={sym.description}
                  >
                    {sym.label}
                  </button>
                ))}
              </div>
              <p className="text-xs text-[var(--text-muted)] mt-2">
                Creates kaleidoscope effect by repeating frequency patterns around the circle
              </p>
            </div>

            {/* Rotation (for circular types only) */}
            {(viz.type === 'circular' || viz.type === 'circular-bars') && (
              <div>
                <h4 className="text-sm font-semibold text-[var(--text-primary)] mb-3">
                  Rotation
                </h4>
                <div className="space-y-3">
                  <Switch
                    label="Enable Rotation"
                    checked={viz.rotate}
                    onChange={(val) => handleVisualizerChange('rotate', val)}
                    description="Continuously rotate the visualizer around the album art"
                  />
                  {viz.rotate && (
                    <>
                      <div>
                        <label className="text-sm text-[var(--text-secondary)] mb-2 block">
                          Rotation Speed: {viz.rotationSpeed}
                        </label>
                        <input
                          type="range"
                          min="0"
                          max="100"
                          value={viz.rotationSpeed}
                          onChange={(e) => handleVisualizerChange('rotationSpeed', parseInt(e.target.value))}
                          className="volume-slider rounded-lg w-full"
                          style={{
                            background: `linear-gradient(to right, var(--accent-color) ${viz.rotationSpeed}%, var(--border-color) ${viz.rotationSpeed}%)`
                          }}
                        />
                      </div>
                      <div>
                        <label className="text-sm text-[var(--text-secondary)] mb-2 block">
                          Direction
                        </label>
                        <div className="grid grid-cols-2 gap-2 p-1 rounded-lg bg-[var(--bg-tertiary)]">
                          {[
                            { value: 'clockwise', label: 'Clockwise' },
                            { value: 'counterclockwise', label: 'Counter-CW' }
                          ].map(dir => (
                            <button
                              key={dir.value}
                              onClick={() => handleVisualizerChange('rotationDirection', dir.value)}
                              className={`px-3 py-2 text-sm font-semibold rounded-md transition-colors ${
                                viz.rotationDirection === dir.value
                                  ? 'bg-[var(--accent-color)] accent-button-text'
                                  : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary-hover)]'
                              }`}
                            >
                              {dir.label}
                            </button>
                          ))}
                        </div>
                      </div>
                    </>
                  )}
                </div>
              </div>
            )}

            {/* Mirror & Invert (for circular and radial) */}
            {(viz.type === 'circular' || viz.type === 'circular-bars') && (
              <div>
                <h4 className="text-sm font-semibold text-[var(--text-primary)] mb-3">
                  Pattern Options
                </h4>
                <div className="space-y-3">
                  <Switch
                    label="Mirror Mode"
                    checked={viz.mirror}
                    onChange={(val) => handleVisualizerChange('mirror', val)}
                    description="Highs in center, lows on edges (mirrored)"
                  />
                  {viz.mirror && (
                    <Switch
                      label="Invert Mirror"
                      checked={viz.invert}
                      onChange={(val) => handleVisualizerChange('invert', val)}
                      description="Lows in center, highs on edges (inverted)"
                    />
                  )}
                </div>
              </div>
            )}

            {/* Mirror & Taper (for bars/waveform only) */}
            {(viz.type === 'bars' || viz.type === 'waveform') && (
              <div>
                <h4 className="text-sm font-semibold text-[var(--text-primary)] mb-3">
                  Layout Options
                </h4>
                <div className="space-y-3">
                  <Switch
                    label="Mirror Mode"
                    checked={viz.mirror}
                    onChange={(val) => handleVisualizerChange('mirror', val)}
                    description="Highs in center, lows on edges (mirrored)"
                  />
                  {viz.mirror && (
                    <Switch
                      label="Invert Mirror"
                      checked={viz.invert}
                      onChange={(val) => handleVisualizerChange('invert', val)}
                      description="Lows in center, highs on edges (inverted)"
                    />
                  )}
                  <Switch
                    label="Taper Edges"
                    checked={viz.taper}
                    onChange={(val) => handleVisualizerChange('taper', val)}
                    description="Fade bars/spectrum at edges for smooth appearance"
                  />
                </div>
              </div>
            )}

            {/* Flip Control (for mixed type only) */}
            {viz.type === 'mixed' && (
              <div>
                <h4 className="text-sm font-semibold text-[var(--text-primary)] mb-3">
                  Mixed Layout
                </h4>
                <Switch
                  label="Flip Layout"
                  checked={viz.mixedFlip}
                  onChange={(val) => handleVisualizerChange('mixedFlip', val)}
                  description="false = bars top / wave bottom, true = wave top / bars bottom"
                />
              </div>
            )}

            {/* Advanced Options (Collapsed) */}
            <div className="pt-4 border-t border-[var(--border-color)]">
              <button
                onClick={() => setShowAdvanced(!showAdvanced)}
                className="flex items-center justify-between w-full text-sm font-semibold text-[var(--text-primary)] hover:text-[var(--accent-color)] transition-colors"
              >
                <span>Advanced Options</span>
                <span className="text-xs">{showAdvanced ? '▼' : '▶'}</span>
              </button>

              {showAdvanced && (
                <div className="mt-4 space-y-4 pl-4">
                  {/* Bass/Treble Analysis */}
                  <div>
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={viz.advanced.bassAnalysis}
                        onChange={(e) => handleAdvancedChange('bassAnalysis', e.target.checked)}
                        className="w-4 h-4 rounded border-[var(--border-color)] bg-[var(--bg-tertiary)] checked:bg-[var(--accent-color)]"
                      />
                      <span className="text-sm text-[var(--text-primary)]">
                        Enable Bass/Treble Separation
                      </span>
                    </label>
                    <p className="text-xs text-[var(--text-muted)] mt-1 ml-6">
                      Different visual effects based on frequency range
                    </p>
                  </div>

                  {/* Particle Effects */}
                  <div>
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={viz.advanced.particles}
                        onChange={(e) => handleAdvancedChange('particles', e.target.checked)}
                        className="w-4 h-4 rounded border-[var(--border-color)] bg-[var(--bg-tertiary)] checked:bg-[var(--accent-color)]"
                      />
                      <span className="text-sm text-[var(--text-primary)]">
                        Enable Particle Effects
                      </span>
                    </label>
                    <p className="text-xs text-[var(--text-muted)] mt-1 ml-6">
                      Adds particle system to visualization
                    </p>
                  </div>
                </div>
              )}
            </div>
          </div>
      </div>

      <div className="pt-4 mt-6 border-t border-[var(--border-color)]">
        <h4 className="text-sm font-semibold text-[var(--text-primary)] mb-2">
          About the Visualizer
        </h4>
        <p className="text-xs text-[var(--text-muted)]">
          The audio visualizer creates real-time visual effects synchronized with your music.
          Colors automatically follow your configured theme accent color.
          Access the visualizer at <span className="font-mono text-[var(--accent-color)]">/visualizer</span> or click
          the "Open Visualizer" button above. Supports fullscreen mode (F key or button) and keyboard
          shortcuts (Space = play/pause, Esc = exit, Arrow keys = volume).
        </p>
      </div>
    </div>
  );
};
