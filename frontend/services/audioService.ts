/**
 * Audio output device discovery and selection.
 *
 * Two things about this API shape matter to the UI and are easy to get wrong:
 *
 * `id` is the stable identifier (`<card_name>:<device>`, e.g. `sndrpihifiberry:0`) and is what gets
 * persisted. `hwId` is the CURRENT ALSA address (`hw:2,0`) and is display-only — ALSA card numbers
 * move across reboots (measured: a HiFiBerry went from card 2 to card 1 across one restart), so
 * keying anything off it will eventually address the wrong card.
 *
 * Saving a device does not mean it is playing. The config API writes settings.json; the player picks
 * the change up on its own poll a moment later and echoes back what it ACTUALLY opened. Until those
 * agree, `pending` is true — and it STAYS true if the device turned out not to open, which is the
 * case the UI must never draw as success. See CurrentOutput.playingOn.
 */

import type {IconName} from '../components/Icon';

const API_BASE = '/api/audio';

export enum DeviceType {
  BUILTIN_HEADPHONES = 'BUILTIN_HEADPHONES',
  BUILTIN_HDMI = 'BUILTIN_HDMI',
  USB = 'USB',
  HAT = 'HAT',
  OTHER = 'OTHER',
}

export interface AudioDevice {
  /** Stable identity — persist and compare on this. */
  id: string;
  /** Current ALSA address; display only, moves across reboots. */
  hwId: string;
  friendlyName: string;
  type: DeviceType;
  isAvailable: boolean;
  /** Why it cannot be selected, when isAvailable is false. */
  unavailableReason: string | null;
  /** Something holds the PCM — normally this unit's own player. */
  inUse: boolean;
  /** This is the output the unit is configured to render to. */
  isActive: boolean;
}

export interface CurrentOutput {
  /** What settings.json (or PLUM_DAC_DEVICE) asks for. */
  configured: string | null;
  /**
   * What the player reports it actually has OPEN. Differs from `configured` while a switch is in
   * flight, and keeps differing if the switch failed and the player fell back.
   */
  playingOn: string | null;
  pending: boolean;
  /** Whether `configured` names a device present on this unit at all. */
  resolved: boolean;
  friendlyName: string;
  unavailableReason: string | null;
}

interface RawDevice {
  id: string;
  hw_id: string;
  friendly_name: string;
  type: DeviceType;
  is_available: boolean;
  unavailable_reason: string | null;
  in_use: boolean;
  is_active: boolean;
}

const toDevice = (d: RawDevice): AudioDevice => ({
  id: d.id,
  hwId: d.hw_id,
  friendlyName: d.friendly_name,
  type: d.type,
  isAvailable: d.is_available,
  unavailableReason: d.unavailable_reason,
  inUse: d.in_use,
  isActive: d.is_active,
});

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.error || body.message || `HTTP ${response.status}`);
  }
  return body as T;
}

export const audioService = {
  async getOutputDevices(): Promise<AudioDevice[]> {
    const raw = await request<RawDevice[]>('/devices/output');
    return raw.map(toDevice);
  },

  async getCurrentOutput(): Promise<CurrentOutput> {
    const raw = await request<{
      configured: string | null;
      playing_on: string | null;
      pending: boolean;
      resolved: boolean;
      friendly_name: string;
      unavailable_reason: string | null;
    }>('/output/current');
    return {
      configured: raw.configured,
      playingOn: raw.playing_on,
      pending: raw.pending,
      resolved: raw.resolved,
      friendlyName: raw.friendly_name,
      unavailableReason: raw.unavailable_reason,
    };
  },

  /** Persist a device. Resolves once SAVED — the switch itself lands a moment later. */
  async setOutputDevice(id: string): Promise<{message: string}> {
    return request<{message: string}>('/output/device', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id}),
    });
  },

  /** Play a test tone. Refused for the device already in use — see the backend's test_device. */
  async testOutputDevice(id: string): Promise<{message: string}> {
    return request<{message: string}>('/output/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id}),
    });
  },

  getDeviceTypeLabel(type: DeviceType): string {
    switch (type) {
      case DeviceType.BUILTIN_HEADPHONES: return 'Built-in';
      case DeviceType.BUILTIN_HDMI: return 'HDMI';
      case DeviceType.USB: return 'USB';
      case DeviceType.HAT: return 'HAT';
      default: return 'Other';
    }
  },

  getDeviceTypeIcon(type: DeviceType): IconName {
    switch (type) {
      case DeviceType.BUILTIN_HEADPHONES: return 'headphones';
      case DeviceType.BUILTIN_HDMI: return 'desktop';
      case DeviceType.USB: return 'volume-high';
      case DeviceType.HAT: return 'waveform';
      default: return 'volume-high';
    }
  },
};
