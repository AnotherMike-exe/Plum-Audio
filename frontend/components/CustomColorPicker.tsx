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
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-xs"
      onClick={onCancel}
      role="dialog"
      aria-modal="true"
      aria-labelledby="color-picker-title"
    >
      <div
        className="relative w-[320px] bg-(--bg-secondary) rounded-2xl shadow-2xl border border-(--border-color) p-6"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h3 id="color-picker-title" className="text-lg font-semibold text-(--text-primary)">
            Custom Color
          </h3>
          <button
            onClick={onCancel}
            className="w-8 h-8 flex items-center justify-center rounded-full text-(--text-secondary) hover:bg-(--bg-tertiary)"
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
            <label className="block text-sm font-medium text-(--text-secondary) mb-2">
              Hex Code
            </label>
            <input
              type="text"
              value={hexInput}
              onChange={handleHexInputChange}
              placeholder="#RRGGBB"
              className="w-full px-3 py-2 bg-(--bg-tertiary) border border-(--border-color) rounded-lg text-(--text-primary) placeholder-(--text-muted) focus:outline-hidden focus:ring-2 focus:ring-(--accent-color)"
              maxLength={7}
            />
            {!/^#[0-9A-Fa-f]{6}$/.test(hexInput) && hexInput.length > 0 && (
              <p className="text-xs text-red-400 mt-1">Invalid hex format (use #RRGGBB)</p>
            )}
          </div>

          {/* Preview Swatch */}
          <div>
            <label className="block text-sm font-medium text-(--text-secondary) mb-2">
              Preview
            </label>
            <div
              className="w-full h-12 rounded-lg border-2 border-(--border-color)"
              style={{ backgroundColor: color }}
            />
          </div>

          {/* Action Buttons */}
          <div className="flex gap-3 pt-2">
            <button
              onClick={onCancel}
              className="flex-1 px-4 py-2 bg-(--bg-tertiary) text-(--text-primary) rounded-lg hover:bg-(--bg-tertiary-hover) transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleApply}
              disabled={!/^#[0-9A-Fa-f]{6}$/.test(color)}
              className="flex-1 px-4 py-2 bg-(--accent-color) accent-button-text rounded-lg hover:bg-(--accent-color-hover) transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Apply
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
