/**
 * Color Contrast Utilities
 * Calculate relative luminance and determine optimal text color for backgrounds
 */

/**
 * Calculate relative luminance of a color (WCAG formula)
 * https://www.w3.org/TR/WCAG20-TECHS/G17.html
 */
export function getRelativeLuminance(hex: string): number {
  // Remove # if present
  const color = hex.replace('#', '');

  // Parse RGB values
  const r = parseInt(color.substring(0, 2), 16) / 255;
  const g = parseInt(color.substring(2, 4), 16) / 255;
  const b = parseInt(color.substring(4, 6), 16) / 255;

  // Apply gamma correction
  const rLinear = r <= 0.03928 ? r / 12.92 : Math.pow((r + 0.055) / 1.055, 2.4);
  const gLinear = g <= 0.03928 ? g / 12.92 : Math.pow((g + 0.055) / 1.055, 2.4);
  const bLinear = b <= 0.03928 ? b / 12.92 : Math.pow((b + 0.055) / 1.055, 2.4);

  // Calculate relative luminance
  return 0.2126 * rLinear + 0.7152 * gLinear + 0.0722 * bLinear;
}

/**
 * Determine if a color is light or dark
 * Returns true if the color is light (needs dark text)
 * Returns false if the color is dark (needs light text)
 *
 * Uses WCAG threshold of 0.5 for relative luminance
 */
export function isLightColor(hex: string): boolean {
  const luminance = getRelativeLuminance(hex);
  // Threshold of 0.5 gives good results for most colors
  // Above 0.5 = light color (use dark text)
  // Below 0.5 = dark color (use light text)
  return luminance > 0.5;
}

/**
 * Get the optimal text color (white or black) for a given background color
 */
export function getTextColorForBackground(backgroundColor: string): string {
  return isLightColor(backgroundColor) ? '#000000' : '#ffffff';
}

/**
 * Calculate contrast ratio between two colors
 * Used for WCAG compliance checking
 */
export function getContrastRatio(color1: string, color2: string): number {
  const l1 = getRelativeLuminance(color1);
  const l2 = getRelativeLuminance(color2);

  const lighter = Math.max(l1, l2);
  const darker = Math.min(l1, l2);

  return (lighter + 0.05) / (darker + 0.05);
}

/**
 * HSL Color Manipulation Utilities
 * For programmatic color adjustment (darken/lighten)
 */

interface HSL {
  h: number; // Hue: 0-360
  s: number; // Saturation: 0-100
  l: number; // Lightness: 0-100
}

/**
 * Convert hex color to HSL
 * @param hex - Hex color string (e.g., "#ff5733")
 * @returns HSL object {h, s, l}
 */
export function hexToHSL(hex: string): HSL {
  // Remove # if present
  const color = hex.replace('#', '');

  // Parse RGB values (0-1 range)
  const r = parseInt(color.substring(0, 2), 16) / 255;
  const g = parseInt(color.substring(2, 4), 16) / 255;
  const b = parseInt(color.substring(4, 6), 16) / 255;

  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const diff = max - min;

  // Calculate lightness
  const l = (max + min) / 2;

  // Calculate saturation
  let s = 0;
  if (diff !== 0) {
    s = l > 0.5 ? diff / (2 - max - min) : diff / (max + min);
  }

  // Calculate hue
  let h = 0;
  if (diff !== 0) {
    switch (max) {
      case r:
        h = ((g - b) / diff + (g < b ? 6 : 0)) / 6;
        break;
      case g:
        h = ((b - r) / diff + 2) / 6;
        break;
      case b:
        h = ((r - g) / diff + 4) / 6;
        break;
    }
  }

  return {
    h: Math.round(h * 360),
    s: Math.round(s * 100),
    l: Math.round(l * 100),
  };
}

/**
 * Convert HSL to hex color
 * @param hsl - HSL object {h, s, l}
 * @returns Hex color string (e.g., "#ff5733")
 */
export function hslToHex(hsl: HSL): string {
  const h = hsl.h / 360;
  const s = hsl.s / 100;
  const l = hsl.l / 100;

  let r, g, b;

  if (s === 0) {
    // Achromatic (gray)
    r = g = b = l;
  } else {
    const hue2rgb = (p: number, q: number, t: number): number => {
      if (t < 0) t += 1;
      if (t > 1) t -= 1;
      if (t < 1 / 6) return p + (q - p) * 6 * t;
      if (t < 1 / 2) return q;
      if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
      return p;
    };

    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;

    r = hue2rgb(p, q, h + 1 / 3);
    g = hue2rgb(p, q, h);
    b = hue2rgb(p, q, h - 1 / 3);
  }

  const toHex = (x: number): string => {
    const hex = Math.round(x * 255).toString(16);
    return hex.length === 1 ? '0' + hex : hex;
  };

  return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
}

/**
 * Darken a color by reducing its lightness
 * @param hex - Hex color string
 * @param amount - Amount to darken (0-100, default 10)
 * @returns Darkened hex color string
 */
export function darkenColor(hex: string, amount: number = 10): string {
  const hsl = hexToHSL(hex);
  hsl.l = Math.max(0, hsl.l - amount);
  return hslToHex(hsl);
}

/**
 * Lighten a color by increasing its lightness
 * @param hex - Hex color string
 * @param amount - Amount to lighten (0-100, default 10)
 * @returns Lightened hex color string
 */
export function lightenColor(hex: string, amount: number = 10): string {
  const hsl = hexToHSL(hex);
  hsl.l = Math.min(100, hsl.l + amount);
  return hslToHex(hsl);
}
