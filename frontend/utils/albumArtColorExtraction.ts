/**
 * Album Art Color Extraction Utility
 * Extracts dual colors (background + accent) from album artwork using ColorThief
 * Ensures WCAG AA contrast compliance (4.5:1 minimum)
 */

import ColorThief from 'colorthief';
import { getContrastRatio, darkenColor, lightenColor, hexToHSL } from './colorContrast';

export interface DualColorExtractionResult {
  backgroundColor: string;
  accentColor: string;
  backgroundLuminance: number;
  accentLuminance: number;
  contrastRatio: number;
  isDarkTheme: boolean;
  source: 'vibrant' | 'muted' | 'fallback';
}

interface RGBColor {
  r: number;
  g: number;
  b: number;
}

interface AnalyzedColor {
  hex: string;
  rgb: RGBColor;
  hsl: { h: number; s: number; l: number };
  saturation: number;
  lightness: number;
  isMuted: boolean;
  isVibrant: boolean;
}

/**
 * Extract dual colors (background + accent) from album art URL
 * Theme-aware color selection with guaranteed WCAG AA contrast (4.5:1 minimum)
 *
 * @param imageUrl - URL to album artwork
 * @param isDarkTheme - Whether the current theme is dark (affects color selection)
 * @param fallbackAccent - Fallback accent color if extraction fails
 * @returns DualColorExtractionResult or null if extraction fails
 */
export async function extractDualColorsFromAlbumArt(
  imageUrl: string,
  isDarkTheme: boolean,
  fallbackAccent: string
): Promise<DualColorExtractionResult | null> {
  try {
    // Validate URL
    if (!imageUrl || typeof imageUrl !== 'string' || imageUrl.trim() === '') {
      throw new Error('Invalid or empty image URL');
    }

    // Skip if placeholder/default artwork (SVG data URLs)
    if (imageUrl.startsWith('data:image/svg+xml')) {
      throw new Error('Placeholder artwork - skipping extraction');
    }

    // Validate data URLs (common issue with corrupted metadata)
    if (imageUrl.startsWith('data:')) {
      const dataUrlRegex = /^data:image\/(jpeg|jpg|png|gif|webp);base64,([A-Za-z0-9+/=]+)$/;
      if (!dataUrlRegex.test(imageUrl)) {
        throw new Error('Malformed data URL - skipping extraction');
      }
    }

    // Load image
    const img = await loadImage(imageUrl);

    // Extract palette using ColorThief
    const colorThief = new ColorThief();
    const palette = colorThief.getPalette(img, 10); // Get 10 dominant colors

    if (!palette || palette.length === 0) {
      throw new Error('No colors extracted from image');
    }

    // Analyze colors to classify them as vibrant or muted
    const analyzedColors = palette.map((rgb) => analyzeColor(rgb));

    // Select background and accent colors based on theme
    const { backgroundColor, accentColor, source } = selectColors(
      analyzedColors,
      isDarkTheme
    );

    if (!backgroundColor || !accentColor) {
      throw new Error('Could not select suitable colors from palette');
    }

    // Validate contrast ratio (WCAG AA minimum: 4.5:1)
    const MIN_CONTRAST_RATIO = 4.5;
    let contrastRatio = getContrastRatio(backgroundColor, accentColor);

    // If contrast is insufficient, try to adjust programmatically
    let finalBgColor = backgroundColor;
    let finalAccentColor = accentColor;

    if (contrastRatio < MIN_CONTRAST_RATIO) {
      const adjustedColors = adjustColorsForContrast(
        backgroundColor,
        accentColor,
        isDarkTheme,
        MIN_CONTRAST_RATIO
      );

      if (adjustedColors) {
        finalBgColor = adjustedColors.backgroundColor;
        finalAccentColor = adjustedColors.accentColor;
        contrastRatio = adjustedColors.contrastRatio;
      } else {
        // If adjustment fails, fall back to user's selected accent
        console.warn('[AlbumArtColor] Could not achieve sufficient contrast, using fallback');
        return null;
      }
    }

    // Calculate luminance values for reference
    const backgroundLuminance = getLuminanceFromHex(finalBgColor);
    const accentLuminance = getLuminanceFromHex(finalAccentColor);

    return {
      backgroundColor: finalBgColor,
      accentColor: finalAccentColor,
      backgroundLuminance,
      accentLuminance,
      contrastRatio,
      isDarkTheme,
      source,
    };
  } catch (error) {
    console.warn('[AlbumArtColor] Extraction failed:', error);
    return null;
  }
}

/**
 * Load image from URL with CORS support and validation
 */
function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.crossOrigin = 'Anonymous';

    img.onload = () => {
      // Verify image loaded successfully
      if (img.naturalWidth === 0 || img.naturalHeight === 0) {
        reject(new Error('Image loaded but has zero dimensions'));
        return;
      }
      resolve(img);
    };

    img.onerror = (error) => {
      console.warn('[AlbumArtColor] Image load error:', error);
      reject(new Error('Failed to load image'));
    };

    // Set source last to trigger load
    img.src = url;
  });
}

/**
 * Analyze a color to determine its properties
 */
function analyzeColor(rgb: [number, number, number]): AnalyzedColor {
  const [r, g, b] = rgb;
  const hex = rgbToHex(r, g, b);
  const hsl = hexToHSL(hex);

  // Classify based on saturation and lightness
  // Muted: Low saturation (< 40%) or very light/dark
  // Vibrant: High saturation (> 40%) and moderate lightness (20-80%)
  const isMuted = hsl.s < 40 || hsl.l < 20 || hsl.l > 80;
  const isVibrant = hsl.s > 40 && hsl.l > 20 && hsl.l < 80;

  return {
    hex,
    rgb: { r, g, b },
    hsl,
    saturation: hsl.s,
    lightness: hsl.l,
    isMuted,
    isVibrant,
  };
}

/**
 * Select background and accent colors from analyzed palette
 */
function selectColors(
  colors: AnalyzedColor[],
  isDarkTheme: boolean
): { backgroundColor: string | null; accentColor: string | null; source: 'vibrant' | 'muted' } {
  // Sort colors by lightness
  const sortedByLightness = [...colors].sort((a, b) => a.lightness - b.lightness);

  let backgroundColor: string | null = null;
  let accentColor: string | null = null;
  let source: 'vibrant' | 'muted' = 'muted';

  if (isDarkTheme) {
    // Dark theme: Look for dark muted color for background, vibrant for accent
    // Background: Dark (lightness < 30%), prefer muted
    const darkColors = sortedByLightness.filter((c) => c.lightness < 30);
    const darkMuted = darkColors.find((c) => c.isMuted);
    backgroundColor = darkMuted?.hex || darkColors[0]?.hex || sortedByLightness[0].hex;

    // Accent: Vibrant, prefer brighter colors
    const vibrantColors = colors.filter((c) => c.isVibrant);
    const brightVibrant = vibrantColors.sort((a, b) => b.lightness - a.lightness);
    accentColor = brightVibrant[0]?.hex;

    // Fallback: If no vibrant, use brightest color
    if (!accentColor) {
      accentColor = sortedByLightness[sortedByLightness.length - 1].hex;
    }
  } else {
    // Light theme: Look for light muted color for background, dark vibrant for accent
    // Background: Light (lightness > 70%), prefer muted
    const lightColors = sortedByLightness.filter((c) => c.lightness > 70);
    const lightMuted = lightColors.find((c) => c.isMuted);
    backgroundColor = lightMuted?.hex || lightColors[lightColors.length - 1]?.hex || sortedByLightness[sortedByLightness.length - 1].hex;

    // Accent: Vibrant, prefer darker colors
    const vibrantColors = colors.filter((c) => c.isVibrant);
    const darkVibrant = vibrantColors.sort((a, b) => a.lightness - b.lightness);
    accentColor = darkVibrant[0]?.hex;

    // Fallback: If no vibrant, use darkest color
    if (!accentColor) {
      accentColor = sortedByLightness[0].hex;
    }
  }

  return { backgroundColor, accentColor, source };
}

/**
 * Convert RGB to hex
 */
function rgbToHex(r: number, g: number, b: number): string {
  return `#${((1 << 24) + (r << 16) + (g << 8) + b).toString(16).slice(1)}`;
}

/**
 * Adjust colors to meet minimum contrast ratio
 * Iteratively darkens/lightens accent color to achieve target contrast
 *
 * @param bgColor - Background color
 * @param accentColor - Accent color
 * @param isDarkTheme - Whether the theme is dark
 * @param minContrast - Minimum contrast ratio (default 4.5 for WCAG AA)
 * @returns Adjusted colors or null if adjustment fails
 */
function adjustColorsForContrast(
  bgColor: string,
  accentColor: string,
  isDarkTheme: boolean,
  minContrast: number = 4.5
): { backgroundColor: string; accentColor: string; contrastRatio: number } | null {
  const MAX_ITERATIONS = 10;
  const ADJUSTMENT_STEP = 10; // Adjust lightness by 10% each iteration

  let adjustedAccent = accentColor;
  let currentContrast = getContrastRatio(bgColor, adjustedAccent);

  for (let i = 0; i < MAX_ITERATIONS; i++) {
    if (currentContrast >= minContrast) {
      return {
        backgroundColor: bgColor,
        accentColor: adjustedAccent,
        contrastRatio: currentContrast,
      };
    }

    // Adjust accent color based on theme
    // Dark theme: lighten accent to stand out against dark background
    // Light theme: darken accent to stand out against light background
    adjustedAccent = isDarkTheme
      ? lightenColor(adjustedAccent, ADJUSTMENT_STEP)
      : darkenColor(adjustedAccent, ADJUSTMENT_STEP);

    currentContrast = getContrastRatio(bgColor, adjustedAccent);
  }

  // If we exhausted iterations without success, return null
  return null;
}

/**
 * Helper: Get relative luminance from hex color
 * Simplified version for internal use
 */
function getLuminanceFromHex(hex: string): number {
  const color = hex.replace('#', '');
  const r = parseInt(color.substring(0, 2), 16) / 255;
  const g = parseInt(color.substring(2, 4), 16) / 255;
  const b = parseInt(color.substring(4, 6), 16) / 255;

  const rLinear = r <= 0.03928 ? r / 12.92 : Math.pow((r + 0.055) / 1.055, 2.4);
  const gLinear = g <= 0.03928 ? g / 12.92 : Math.pow((g + 0.055) / 1.055, 2.4);
  const bLinear = b <= 0.03928 ? b / 12.92 : Math.pow((b + 0.055) / 1.055, 2.4);

  return 0.2126 * rLinear + 0.7152 * gLinear + 0.0722 * bLinear;
}
