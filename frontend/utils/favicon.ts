/**
 * Favicon Utility
 * Generates dynamic favicons using the Snapcast icon with custom accent colors
 */

import { getTextColorForBackground } from './colorContrast';

const SNAPCAST_ICON = `<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="1000" viewBox="0 0 937.5 937.5">
  <g transform="translate(0 -114.862)">
    <circle cx="-739.798" cy="114.143" r="431.25" style="fill:ACCENT_COLOR;fill-opacity:1;stroke-width:10.48156548" transform="rotate(-120)"/>
    <path fill="none" d="M-427.771-63.976a360.3 360.3 0 0 1 0 360.297M-472.58-20.016a299.98 299.98 0 0 1 0 272.378m-47.884-233.873a240.17 240.17 0 0 1 0 195.368" style="fill:#000;fill-opacity:0;fill-rule:evenodd;stroke:STROKE_COLOR;stroke-width:34.99999619;stroke-linecap:round;stroke-linejoin:miter;stroke-miterlimit:4;stroke-dasharray:none;stroke-opacity:1" transform="rotate(-120)"/>
    <path d="m580.047 420.343-114.36 98.912H357.453v127.287h106.594l116 100.34z" style="fill:STROKE_COLOR;fill-opacity:1;fill-rule:evenodd;stroke:STROKE_COLOR;stroke-width:33.33299637;stroke-linecap:round;stroke-linejoin:round;stroke-miterlimit:4;stroke-dasharray:none;stroke-opacity:1"/>
    <path fill="none" d="M780.777 405.493a360.3 360.3 0 0 1 0 360.297m-44.741-316.337a299.98 299.98 0 0 1 0 272.378m-47.884-233.873a240.17 240.17 0 0 1 0 195.368" style="fill:#000;fill-opacity:0;fill-rule:evenodd;stroke:STROKE_COLOR;stroke-width:34.99999619;stroke-linecap:round;stroke-linejoin:miter;stroke-miterlimit:4;stroke-dasharray:none;stroke-opacity:1"/>
    <path fill="none" d="M583.007-875.875a360.3 360.3 0 0 1 0 360.297m-44.741-316.337a299.98 299.98 0 0 1 0 272.378M490.383-793.41a240.17 240.17 0 0 1 0 195.368" style="fill:#000;fill-opacity:0;fill-rule:evenodd;stroke:STROKE_COLOR;stroke-width:34.99999619;stroke-linecap:round;stroke-linejoin:miter;stroke-miterlimit:4;stroke-dasharray:none;stroke-opacity:1" transform="rotate(120)"/>
  </g>
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
 * Update the browser favicon with the Snapcast icon in the specified accent color
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
  const svg = SNAPCAST_ICON
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
