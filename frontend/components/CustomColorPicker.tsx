import React, { useState } from 'react';
import { HexColorPicker } from 'react-colorful';
import { Icon } from './Icon';

interface CustomColorPickerProps {
  initialColor: string;
  onApply: (color: string) => void;
  onCancel: () => void;
}

export const CustomColorPicker: React.FC<CustomColorPickerProps> = ({
  initialColor,
  onApply,
  onCancel,
}) => {
  const [color, setColor] = useState(initialColor);
  const [hexInput, setHexInput] = useState(initialColor);

  const handleColorChange = (newColor: string) => {
    setColor(newColor);
    setHexInput(newColor);
  };

  const handleHexInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const value = e.target.value;
    setHexInput(value);

    // Validate hex format: #RRGGBB
    if (/^#[0-9A-Fa-f]{6}$/.test(value)) {
      setColor(value);
    }
  };

  const handleApply = () => {
    // Ensure valid hex before applying
    if (/^#[0-9A-Fa-f]{6}$/.test(color)) {
      onApply(color);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={onCancel}
      role="dialog"
      aria-modal="true"
      aria-labelledby="color-picker-title"
    >
      <div
        className="relative w-[320px] bg-[var(--bg-secondary)] rounded-2xl shadow-2xl border border-[var(--border-color)] p-6"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h3 id="color-picker-title" className="text-lg font-semibold text-[var(--text-primary)]">
            Custom Color
          </h3>
          <button
            onClick={onCancel}
            className="w-8 h-8 flex items-center justify-center rounded-full text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]"
            aria-label="Close color picker"
          >
            <Icon name="xmark" />
          </button>
        </div>

        <div className="space-y-4">
          {/* Color Picker */}
          <div className="flex justify-center">
            <HexColorPicker color={color} onChange={handleColorChange} />
          </div>

          {/* Hex Input */}
          <div>
            <label className="block text-sm font-medium text-[var(--text-secondary)] mb-2">
              Hex Code
            </label>
            <input
              type="text"
              value={hexInput}
              onChange={handleHexInputChange}
              placeholder="#RRGGBB"
              className="w-full px-3 py-2 bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:outline-none focus:ring-2 focus:ring-[var(--accent-color)]"
              maxLength={7}
            />
            {!/^#[0-9A-Fa-f]{6}$/.test(hexInput) && hexInput.length > 0 && (
              <p className="text-xs text-red-400 mt-1">Invalid hex format (use #RRGGBB)</p>
            )}
          </div>

          {/* Preview Swatch */}
          <div>
            <label className="block text-sm font-medium text-[var(--text-secondary)] mb-2">
              Preview
            </label>
            <div
              className="w-full h-12 rounded-lg border-2 border-[var(--border-color)]"
              style={{ backgroundColor: color }}
            />
          </div>

          {/* Action Buttons */}
          <div className="flex gap-3 pt-2">
            <button
              onClick={onCancel}
              className="flex-1 px-4 py-2 bg-[var(--bg-tertiary)] text-[var(--text-primary)] rounded-lg hover:bg-[var(--bg-tertiary-hover)] transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleApply}
              disabled={!/^#[0-9A-Fa-f]{6}$/.test(color)}
              className="flex-1 px-4 py-2 bg-[var(--accent-color)] accent-button-text rounded-lg hover:bg-[var(--accent-color-hover)] transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Apply
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
