/**
 * About/version service — reports this unit's app version plus the live-queried versions of the
 * pieces it wraps (aiosendspin, shairport-sync, go-librespot, bluez-alsa). Cached for the life of the
 * page: it only changes when the unit is rebuilt/restarted, so refetching on every mount buys nothing.
 */

const API_BASE_URL = '/api/about/versions';

export interface AppVersionInfo {
  version: string;
  buildType: 'release' | 'dev' | string;
  gitDescribe: string | null;
}

export interface VersionInfo {
  app: AppVersionInfo;
  sendspin: { aiosendspin: string | null };
  airplay: { shairportSync: string | null };
  spotify: { goLibrespot: string | null };
  bluetooth: { bluezAlsa: string | null };
}

class AboutService {
  private cached: VersionInfo | null = null;
  private pending: Promise<VersionInfo> | null = null;

  async getVersions(): Promise<VersionInfo> {
    if (this.cached) {
      return this.cached;
    }
    if (!this.pending) {
      this.pending = fetch(API_BASE_URL)
        .then((response) => {
          if (!response.ok) {
            throw new Error('Failed to load version info');
          }
          return response.json();
        })
        .then((data: VersionInfo) => {
          this.cached = data;
          return data;
        })
        .finally(() => {
          this.pending = null;
        });
    }
    return this.pending;
  }
}

export const aboutService = new AboutService();
