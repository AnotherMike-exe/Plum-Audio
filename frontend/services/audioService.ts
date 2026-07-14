/**
 * Audio Service
 * Handles API calls for audio device discovery and configuration
 */

const API_BASE = `${window.location.protocol}//${window.location.hostname}:${window.location.port}/api/audio`;

export enum DeviceType {
  BUILTIN_HEADPHONES = 'BUILTIN_HEADPHONES',
  BUILTIN_HDMI = 'BUILTIN_HDMI',
  USB = 'USB',
  HAT = 'HAT',
  OTHER = 'OTHER'
}

export interface AudioDevice {
  card: number;
  device: number;
  hw_id: string;
  hw_name: string | null;
  card_name: string;
  device_name: string;
  type: DeviceType;
  friendly_name: string;
  is_available: boolean;
}

export interface CurrentOutputDevice {
  hw_id: string;
  hw_name: string | null;
  friendly_name: string;
  type: DeviceType;
  is_available: boolean;
}

export interface SetOutputDeviceResult {
  success: boolean;
  message?: string;
  error?: string;
  device?: {
    hw_id: string;
    friendly_name: string;
  };
  fallback_device?: string;
  details?: string;
}

export interface TestDeviceResult {
  success: boolean;
  message: string;
  device?: {
    hw_id: string;
    friendly_name: string;
  };
}

export interface ConfiguredInputDevice {
  hw_id: string;
  custom_name: string;
  enabled: boolean;
  is_available: boolean;
  device_info: AudioDevice | null;
}

export interface InputDeviceConfigResult {
  success: boolean;
  message?: string;
  error?: string;
  device?: {
    hw_id: string;
    custom_name: string;
    enabled: boolean;
  };
}

export interface ToggleInputDeviceResult {
  success: boolean;
  message?: string;
  error?: string;
  enabled?: boolean;
}

/**
 * Audio Configuration Service
 */
export const audioService = {
  /**
   * Get all available output devices
   */
  async getOutputDevices(): Promise<AudioDevice[]> {
    try {
      const response = await fetch(`${API_BASE}/devices/output`);

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      return await response.json();
    } catch (error) {
      console.error('Failed to get output devices:', error);
      throw error;
    }
  },

  /**
   * Get all available input devices
   */
  async getInputDevices(): Promise<AudioDevice[]> {
    try {
      const response = await fetch(`${API_BASE}/devices/input`);

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      return await response.json();
    } catch (error) {
      console.error('Failed to get input devices:', error);
      throw error;
    }
  },

  /**
   * Get currently configured output device
   */
  async getCurrentOutputDevice(): Promise<CurrentOutputDevice> {
    try {
      const response = await fetch(`${API_BASE}/output/current`);

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      return await response.json();
    } catch (error) {
      console.error('Failed to get current output device:', error);
      throw error;
    }
  },

  /**
   * Set output device
   */
  async setOutputDevice(hwId: string): Promise<SetOutputDeviceResult> {
    try {
      const response = await fetch(`${API_BASE}/output/device`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ hw_id: hwId }),
      });

      const result = await response.json();

      if (!response.ok) {
        throw new Error(result.error || `HTTP ${response.status}`);
      }

      return result;
    } catch (error) {
      console.error('Failed to set output device:', error);
      throw error;
    }
  },

  /**
   * Test an output device (plays brief test sound)
   */
  async testOutputDevice(hwId: string): Promise<TestDeviceResult> {
    try {
      const response = await fetch(`${API_BASE}/output/test`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ hw_id: hwId }),
      });

      const result = await response.json();

      if (!response.ok) {
        throw new Error(result.message || `HTTP ${response.status}`);
      }

      return result;
    } catch (error) {
      console.error('Failed to test output device:', error);
      throw error;
    }
  },

  /**
   * Get friendly device type label
   */
  getDeviceTypeLabel(type: DeviceType): string {
    switch (type) {
      case DeviceType.BUILTIN_HEADPHONES:
        return 'Built-in';
      case DeviceType.BUILTIN_HDMI:
        return 'HDMI';
      case DeviceType.USB:
        return 'USB';
      case DeviceType.HAT:
        return 'HAT';
      case DeviceType.OTHER:
        return 'Other';
      default:
        return 'Unknown';
    }
  },

  /**
   * Get device type icon name
   */
  getDeviceTypeIcon(type: DeviceType): string {
    switch (type) {
      case DeviceType.BUILTIN_HEADPHONES:
        return 'headphones';
      case DeviceType.BUILTIN_HDMI:
        return 'tv';
      case DeviceType.USB:
        return 'usb';
      case DeviceType.HAT:
        return 'cpu';
      case DeviceType.OTHER:
      default:
        return 'speaker';
    }
  },

  /**
   * Get configured input devices
   */
  async getConfiguredInputDevices(): Promise<ConfiguredInputDevice[]> {
    try {
      const response = await fetch(`${API_BASE}/input/devices`);

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      return await response.json();
    } catch (error) {
      console.error('Failed to get configured input devices:', error);
      throw error;
    }
  },

  /**
   * Add or update input device configuration
   */
  async addOrUpdateInputDevice(
    hwId: string,
    customName?: string,
    enabled?: boolean
  ): Promise<InputDeviceConfigResult> {
    try {
      const body: any = { hw_id: hwId };
      if (customName !== undefined) body.custom_name = customName;
      if (enabled !== undefined) body.enabled = enabled;

      const response = await fetch(`${API_BASE}/input/device`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(body),
      });

      const result = await response.json();

      if (!response.ok) {
        throw new Error(result.error || `HTTP ${response.status}`);
      }

      return result;
    } catch (error) {
      console.error('Failed to add/update input device:', error);
      throw error;
    }
  },

  /**
   * Remove input device configuration
   */
  async removeInputDevice(hwId: string): Promise<InputDeviceConfigResult> {
    try {
      const encodedHwId = encodeURIComponent(hwId);
      const response = await fetch(`${API_BASE}/input/device/${encodedHwId}`, {
        method: 'DELETE',
      });

      const result = await response.json();

      if (!response.ok) {
        throw new Error(result.error || `HTTP ${response.status}`);
      }

      return result;
    } catch (error) {
      console.error('Failed to remove input device:', error);
      throw error;
    }
  },

  /**
   * Toggle input device enabled state
   */
  async toggleInputDevice(hwId: string): Promise<ToggleInputDeviceResult> {
    try {
      const encodedHwId = encodeURIComponent(hwId);
      const response = await fetch(`${API_BASE}/input/device/${encodedHwId}/toggle`, {
        method: 'POST',
      });

      const result = await response.json();

      if (!response.ok) {
        throw new Error(result.error || `HTTP ${response.status}`);
      }

      return result;
    } catch (error) {
      console.error('Failed to toggle input device:', error);
      throw error;
    }
  }
};
