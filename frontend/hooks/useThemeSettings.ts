/**
 * useThemeSettings
 *
 * Applies the user's theme/accent choices to the document root as data-attributes
 * and CSS custom properties, and keeps the favicon in sync. Ported from App.tsx's
 * theme-apply effect.
 *
 * When `settings.theme.useAlbumArtColors` is on, this hook also extracts a dual
 * (background + accent) palette from the currently-featured album art and drives
 * the whole UI from it — the same behaviour the original single-unit GUI had. It
 * returns the extracted palette so the caller can hand it to the visualizer.
 */

import {useEffect, useState} from 'react';
import type {Settings} from '../types';
import {updateFavicon} from '../utils/favicon';
import {getTextColorForBackground, lightenColor, darkenColor} from '../utils/colorContrast';
import {
    extractDualColorsFromAlbumArt,
    type DualColorExtractionResult,
} from '../utils/albumArtColorExtraction';

const ACCENT_COLORS: Record<string, string> = {
    purple: '#aa5cc3',
    blue: '#3b82f6',
    green: '#22c55e',
    orange: '#f97316',
    red: '#ef4444',
    yellow: '#eab308',
};

function isPlaceholderArt(url: string): boolean {
    return url.startsWith('data:image/svg+xml');
}

/**
 * @param settings     merged settings (theme lives at settings.theme)
 * @param albumArtUrl  the featured track's artwork, or null when nothing is playing
 * @returns the extracted album-art palette (null when disabled / unavailable)
 */
export function useThemeSettings(
    settings: Settings,
    albumArtUrl?: string | null,
): DualColorExtractionResult | null {
    const theme = settings.theme;
    const [albumColors, setAlbumColors] = useState<DualColorExtractionResult | null>(null);

    // Keep the browser tab title in sync with the device name.
    useEffect(() => {
        if (settings.deviceName) {
            document.title = settings.deviceName;
        }
    }, [settings.deviceName]);

    // Keep the favicon in sync with the accent.
    useEffect(() => {
        updateFavicon(theme.accent, theme.customColor, theme.mode);
    }, [theme.accent, theme.customColor, theme.mode]);

    // Extract dual colors from the current album art when it changes (if enabled).
    useEffect(() => {
        if (!theme.useAlbumArtColors || !albumArtUrl || isPlaceholderArt(albumArtUrl)) {
            setAlbumColors(null);
            return;
        }

        const effectiveMode = theme.mode === 'system'
            ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
            : theme.mode;
        const isDarkTheme = effectiveMode === 'dark' || effectiveMode === 'black';

        const fallbackAccent = theme.accent === 'custom' && theme.customColor
            ? theme.customColor
            : (ACCENT_COLORS[theme.accent] || ACCENT_COLORS.purple);

        let cancelled = false;
        extractDualColorsFromAlbumArt(albumArtUrl, isDarkTheme, fallbackAccent)
            .then((result) => {
                if (!cancelled) setAlbumColors(result ?? null);
            })
            .catch((error) => {
                console.warn('[AlbumArtColor] Extraction failed:', error);
                if (!cancelled) setAlbumColors(null);
            });
        return () => {
            cancelled = true;
        };
    }, [albumArtUrl, theme.useAlbumArtColors, theme.mode, theme.accent, theme.customColor]);

    // Apply theme mode + accent (and album-art palette when active) to the document root.
    useEffect(() => {
        const root = document.documentElement;

        const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
        const handleSystemThemeChange = (e: MediaQueryListEvent) => {
            if (theme.mode === 'system') {
                root.setAttribute('data-theme', e.matches ? 'dark' : 'light');
            }
        };

        if (theme.mode === 'system') {
            root.setAttribute('data-theme', mediaQuery.matches ? 'dark' : 'light');
            mediaQuery.addEventListener('change', handleSystemThemeChange);
        } else {
            root.setAttribute('data-theme', theme.mode);
            mediaQuery.removeEventListener('change', handleSystemThemeChange);
        }

        root.setAttribute('data-accent', theme.accent);

        // Resolve 'system' to the actual mode to detect monochrome modes.
        const effectiveMode = theme.mode === 'system'
            ? (mediaQuery.matches ? 'dark' : 'light')
            : theme.mode;
        const isMonochromeMode = effectiveMode === 'black' || effectiveMode === 'white';

        const BG_TEXT_PROPS = [
            '--bg-primary', '--bg-secondary', '--bg-tertiary', '--bg-secondary-hover',
            '--bg-tertiary-hover', '--border-color', '--text-primary', '--text-secondary',
            '--text-muted', '--icon-muted', '--control-icon-color',
        ];

        if (isMonochromeMode) {
            // Monochrome modes define their own colors in CSS — clear any inline overrides.
            for (const prop of [
                ...BG_TEXT_PROPS, '--accent-text-color', '--accent-color', '--accent-color-hover',
                '--custom-accent-color', '--custom-accent-color-hover',
            ]) {
                root.style.removeProperty(prop);
            }
        } else if (theme.useAlbumArtColors && albumColors) {
            // Album-art path: drive both background and accent from the extracted palette.
            const bgColor = albumColors.backgroundColor;
            const accentColor = albumColors.accentColor;
            const isDark = albumColors.isDarkTheme;

            root.style.setProperty('--bg-primary', bgColor);
            const bgSecondary = lightenColor(bgColor, isDark ? 5 : 3);
            root.style.setProperty('--bg-secondary', bgSecondary);
            const bgTertiary = lightenColor(bgColor, isDark ? 8 : 5);
            root.style.setProperty('--bg-tertiary', bgTertiary);
            root.style.setProperty('--bg-secondary-hover', lightenColor(bgSecondary, isDark ? 5 : 3));
            root.style.setProperty('--bg-tertiary-hover', lightenColor(bgTertiary, isDark ? 5 : 3));
            root.style.setProperty('--border-color', isDark ? lightenColor(bgColor, 15) : darkenColor(bgColor, 10));

            if (isDark) {
                root.style.setProperty('--text-primary', '#f0f0f0');
                root.style.setProperty('--text-secondary', '#b0b0c0');
                root.style.setProperty('--text-muted', '#808090');
                root.style.setProperty('--icon-muted', '#707080');
            } else {
                root.style.setProperty('--text-primary', '#181c32');
                root.style.setProperty('--text-secondary', '#5e6278');
                root.style.setProperty('--text-muted', '#7e8299');
                root.style.setProperty('--icon-muted', '#a1a5b7');
            }

            const accentHover = lightenColor(accentColor, 15);
            root.style.setProperty('--accent-color', accentColor);
            root.style.setProperty('--accent-color-hover', accentHover);
            root.style.setProperty('--custom-accent-color', accentColor);
            root.style.setProperty('--custom-accent-color-hover', accentHover);
            root.style.setProperty('--accent-text-color', getTextColorForBackground(accentColor));
            root.style.setProperty('--control-icon-color', accentColor);
        } else {
            // Built-in / custom accent path — clear background/text overrides.
            for (const prop of BG_TEXT_PROPS) {
                root.style.removeProperty(prop);
            }

            // Priority: custom color > built-in accent.
            const effectiveAccentColor = theme.accent === 'custom' && theme.customColor
                ? theme.customColor
                : (ACCENT_COLORS[theme.accent] || ACCENT_COLORS.purple);

            const accentHover = lightenColor(effectiveAccentColor, 15);
            const textColor = getTextColorForBackground(effectiveAccentColor);

            root.style.setProperty('--accent-color', effectiveAccentColor);
            root.style.setProperty('--accent-color-hover', accentHover);
            root.style.setProperty('--custom-accent-color', effectiveAccentColor);
            root.style.setProperty('--custom-accent-color-hover', accentHover);
            root.style.setProperty('--accent-text-color', textColor);
        }

        return () => {
            mediaQuery.removeEventListener('change', handleSystemThemeChange);
        };
    }, [theme.mode, theme.accent, theme.customColor, theme.useAlbumArtColors, albumColors]);

    return albumColors;
}
