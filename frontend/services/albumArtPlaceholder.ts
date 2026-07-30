/**
 * The one "no album art" image, shared by everything that renders artwork.
 *
 * It is an inline SVG data URI rather than a file, for two reasons: it cannot 404 (so it is safe as
 * an <img> fallback for a URL that failed to load), and `useThemeSettings.isPlaceholderArt()`
 * recognises `data:image/svg+xml` and skips colour extraction — feeding a flat placeholder to
 * ColorThief would repaint the whole UI grey every time a track had no art.
 *
 * WHY THIS EXISTS: sendspinDataService defaulted `albumArtUrl` to an EMPTY STRING when a source
 * published no artwork, and NowPlaying renders it unconditionally — so `<img src="">` drew the
 * browser's broken-image glyph with the alt text beside it. Reported from hardware 2026-07-30 on a
 * Bluetooth reconnect and on any hard refresh, where metadata arrives seconds before artwork; the
 * predecessor's snapcastDataService had a data-URI default and the port dropped it.
 */
import musicNotePlaceholderRaw from '../src/assets/icons/music-note-placeholder.svg?raw';

export const ALBUM_ART_PLACEHOLDER = `data:image/svg+xml,${encodeURIComponent(musicNotePlaceholderRaw)}`;
