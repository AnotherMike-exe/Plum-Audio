/**
 * useThemeSettings
 *
 * Applies the user's theme/accent choices to the document root as data-attributes
 * and CSS custom properties, and keeps the favicon in sync. Ported from App.tsx's
 * theme-apply effect. Album-art-driven accent extraction is deferred to the
 * visualizer slice — this hook only handles the built-in / custom accent path.
 */

import {useEffect} from 'react';
import type {Settings} from '../types';
import {updateFavicon} from '../utils/favicon';
import {getTextColorForBackground, lightenColor} from '../utils/colorContrast';

const ACCENT_COLORS: Record<string, string> = {
    purple: '#aa5cc3',
    blue: '#3b82f6',
    green: '#22c55e',
    orange: '#f97316',
    red: '#ef4444',
    yellow: '#eab308',
};

export function useThemeSettings(settings: Settings): void {
    const theme = settings.theme;

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

    // Apply theme mode + accent to the document root.
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

        if (isMonochromeMode) {
            // Monochrome modes define their own colors in CSS — clear any inline overrides.
            for (const prop of [
                '--accent-text-color', '--bg-primary', '--bg-secondary', '--bg-tertiary',
                '--bg-secondary-hover', '--bg-tertiary-hover', '--border-color',
                '--text-primary', '--text-secondary', '--text-muted', '--icon-muted',
                '--accent-color', '--accent-color-hover', '--custom-accent-color',
                '--custom-accent-color-hover', '--control-icon-color',
            ]) {
                root.style.removeProperty(prop);
            }
        } else {
            // Clear background/text overrides (owned by the album-art path, deferred).
            for (const prop of [
                '--bg-primary', '--bg-secondary', '--bg-tertiary', '--bg-secondary-hover',
                '--bg-tertiary-hover', '--border-color', '--text-primary', '--text-secondary',
                '--text-muted', '--icon-muted', '--control-icon-color',
            ]) {
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
    }, [theme.mode, theme.accent, theme.customColor]);
}
