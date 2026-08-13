/**
 * Favicon Utility
 * Generates dynamic favicons using the Sendspin icon with custom accent colors
 */

import { getTextColorForBackground } from './colorContrast';

// Based on the official Sendspin logo (_resources/Assets/sendspin-favicon.svg).
// The outer disc + center dot carry the accent/theme color; the rings and inner
// disc carry the contrast-toggled color (same role STROKE_COLOR played on the
// old Snapcast-derived icon).
const SENDSPIN_ICON = `<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="1000" viewBox="0 0 64 64">
  <circle cx="32" cy="32" r="32" fill="ACCENT_COLOR"/>
  <circle cx="32" cy="32" r="27" fill="none" stroke="STROKE_COLOR" stroke-width="0.6"/>
  <circle cx="32" cy="32" r="23" fill="none" stroke="STROKE_COLOR" stroke-width="0.6"/>
  <circle cx="32" cy="32" r="19" fill="none" stroke="STROKE_COLOR" stroke-width="0.6"/>
  <circle cx="32" cy="32" r="15" fill="none" stroke="STROKE_COLOR" stroke-width="0.6"/>
  <circle cx="32" cy="32" r="10" fill="STROKE_COLOR"/>
  <circle cx="32" cy="32" r="1.4" fill="ACCENT_COLOR"/>
</svg>`;

/**
 * Accent color to hex mapping
 */
const ACCENT_COLORS: Record<string, string> = {
  purple: '#aa5cc3',
  blue: '#3b82f6',
  green: '#22c55e',
  orange: '#f97316',
  red: '#ef4444',
  yellow: '#eab308',
};

/**
 * Update the browser favicon with the Sendspin icon in the specified accent color
 */
export function updateFavicon(accentColor: string, customColor?: string, themeMode?: string): void {
  let color: string;
  let strokeColor: string;

  // Handle monochrome themes specially
  if (themeMode === 'black') {
    // Black theme: black background with white icon
    color = '#000000';
    strokeColor = '#ffffff';
  } else if (themeMode === 'white') {
    // White theme: white background with black icon
    color = '#ffffff';
    strokeColor = '#000000';
  } else {
    // Regular themes: use accent color
    color = accentColor === 'custom' && customColor
      ? customColor
      : (ACCENT_COLORS[accentColor] || ACCENT_COLORS.purple);

    // Calculate stroke color based on background color for proper contrast
    strokeColor = getTextColorForBackground(color);
  }

  // Replace placeholders with actual colors
  const svg = SENDSPIN_ICON
    .replace(/ACCENT_COLOR/g, color)
    .replace(/STROKE_COLOR/g, strokeColor);

  // Create data URL
  const dataUrl = `data:image/svg+xml,${encodeURIComponent(svg)}`;

  // Find or create favicon link element
  let link = document.querySelector("link[rel='icon']") as HTMLLinkElement;
  if (!link) {
    link = document.createElement('link');
    link.rel = 'icon';
    document.head.appendChild(link);
  }

  // Update the href
  link.href = dataUrl;
}
