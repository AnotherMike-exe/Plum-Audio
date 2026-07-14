/**
 * Device Settings Service
 * Handles device name and hostname configuration
 */

const API_BASE_URL = '/api/settings/device';

export interface DeviceSettings {
  deviceName: string;
  hostname: string;
}

export interface ValidationResult {
  valid: boolean;
  error?: string;
}

export interface UpdateResponse {
  success: boolean;
  message: string;
  settings?: any;
}

class DeviceSettingsService {
  /**
   * Update device name and/or hostname
   */
  async updateDevice(settings: Partial<DeviceSettings>): Promise<UpdateResponse> {
    try {
      const response = await fetch(API_BASE_URL, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(settings),
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.error || 'Failed to update device settings');
      }

      return await response.json();
    } catch (error) {
      console.error('Failed to update device settings:', error);
      throw error;
    }
  }

  /**
   * Validate hostname
   */
  async validateHostname(hostname: string): Promise<ValidationResult> {
    try {
      const response = await fetch(`${API_BASE_URL}/hostname/validate`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ hostname }),
      });

      if (!response.ok) {
        throw new Error('Validation request failed');
      }

      return await response.json();
    } catch (error) {
      console.error('Failed to validate hostname:', error);
      return {
        valid: false,
        error: 'Validation request failed',
      };
    }
  }

  /**
   * Sanitize device name to valid hostname
   */
  async sanitizeHostname(deviceName: string): Promise<string> {
    try {
      const response = await fetch(`${API_BASE_URL}/hostname/sanitize`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ deviceName }),
      });

      if (!response.ok) {
        throw new Error('Sanitization request failed');
      }

      const data = await response.json();
      return data.hostname;
    } catch (error) {
      console.error('Failed to sanitize hostname:', error);
      // Fallback: do basic sanitization client-side
      return deviceName
        .toLowerCase()
        .replace(/[^a-z0-9-]/g, '-')
        .replace(/^-+|-+$/g, '')
        .slice(0, 63) || 'plum-snapcast';
    }
  }
}

// Export singleton instance
export const deviceSettingsService = new DeviceSettingsService();
