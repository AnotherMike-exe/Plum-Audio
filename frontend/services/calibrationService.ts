/**
 * Volume Calibration Service
 *
 * Provides utilities for dB-matched volume control across endpoints.
 * Handles test tone playback, calibration calculations, and settings persistence.
 */

import type {
  EndpointCalibration,
  CalibrationReference,
  CalibrationMaxLimit,
  AudioCalibrationSettings
} from '../types';
import {settingsService} from './settingsService';

// Default calibration for uncalibrated endpoints
export const DEFAULT_CALIBRATION: EndpointCalibration = {
  name: '',
  calibrated: false,
  maxLimit: {mode: 'percentage', value: 100},
  defaultVolume: 80,
};

// Standard calibration measurement points
export const CALIBRATION_LOW_VOLUME = 38;   // ~38% for low reference
export const CALIBRATION_HIGH_VOLUME = 80;  // ~80% for high reference

/**
 * Calculate dB level for a given hardware volume percentage
 * Uses linear interpolation between calibration reference points
 */
export function hardwareVolumeToDb(hardwarePercent: number, cal: EndpointCalibration): number | null {
  if (!cal.calibrated || !cal.lowRef || !cal.highRef) {
    return null;  // Cannot calculate without calibration
  }

  const slope = (cal.highRef.measuredDb - cal.lowRef.measuredDb) /
                (cal.highRef.volume - cal.lowRef.volume);
  const intercept = cal.lowRef.measuredDb - (slope * cal.lowRef.volume);

  return slope * hardwarePercent + intercept;
}

/**
 * Calculate hardware volume percentage needed to achieve a target dB level
 */
export function dbToHardwareVolume(targetDb: number, cal: EndpointCalibration): number | null {
  if (!cal.calibrated || !cal.lowRef || !cal.highRef) {
    return null;
  }

  const slope = (cal.highRef.measuredDb - cal.lowRef.measuredDb) /
                (cal.highRef.volume - cal.lowRef.volume);
  const intercept = cal.lowRef.measuredDb - (slope * cal.lowRef.volume);

  const volume = (targetDb - intercept) / slope;
  return Math.max(0, Math.min(100, volume));
}

/**
 * Get the effective maximum hardware volume for an endpoint
 * If max is set in dB mode, calculate the corresponding hardware %
 */
export function getEffectiveMaxVolume(cal: EndpointCalibration): number {
  if (cal.maxLimit.mode === 'percentage') {
    return cal.maxLimit.value;
  }

  // dB mode: find hardware % that produces target dB
  const hardwarePercent = dbToHardwareVolume(cal.maxLimit.value, cal);
  if (hardwarePercent === null) {
    return 100;  // Fallback if not calibrated
  }

  return Math.min(100, Math.max(0, hardwarePercent));
}

/**
 * Convert user-facing slider position (0-100) to actual hardware volume
 * Slider maps linearly to 0% → maxVolume%
 */
export function sliderToHardware(sliderPercent: number, cal: EndpointCalibration): number {
  const maxVolume = getEffectiveMaxVolume(cal);
  return (sliderPercent * maxVolume) / 100;
}

/**
 * Convert hardware volume to user-facing slider position
 */
export function hardwareToSlider(hardwarePercent: number, cal: EndpointCalibration): number {
  const maxVolume = getEffectiveMaxVolume(cal);
  if (maxVolume === 0) return 0;
  return (hardwarePercent * 100) / maxVolume;
}

/**
 * Calculate the slider volume for a joining endpoint to match another endpoint's dB level
 *
 * @param sourceSlider Current slider position of source endpoint (0-100)
 * @param sourceCal Source endpoint's calibration
 * @param targetCal Target (joining) endpoint's calibration
 * @returns Slider position for target endpoint to match dB, or null if calculation not possible
 */
export function getMatchingSliderVolume(
  sourceSlider: number,
  sourceCal: EndpointCalibration,
  targetCal: EndpointCalibration
): number | null {
  // If either endpoint is uncalibrated, return same slider position
  if (!sourceCal.calibrated || !targetCal.calibrated) {
    return sourceSlider;
  }

  // 1. Source slider → source hardware
  const sourceHardware = sliderToHardware(sourceSlider, sourceCal);

  // 2. Source hardware → dB
  const targetDb = hardwareVolumeToDb(sourceHardware, sourceCal);
  if (targetDb === null) {
    return sourceSlider;
  }

  // 3. dB → target hardware
  const targetHardware = dbToHardwareVolume(targetDb, targetCal);
  if (targetHardware === null) {
    return sourceSlider;
  }

  // 4. Target hardware → target slider
  const targetSlider = hardwareToSlider(targetHardware, targetCal);

  // Clamp to valid range
  return Math.max(0, Math.min(100, Math.round(targetSlider)));
}

/**
 * Get estimated dB range for a calibrated endpoint
 */
export function getDbRange(cal: EndpointCalibration): {minDb: number; maxDb: number} | null {
  if (!cal.calibrated || !cal.lowRef || !cal.highRef) {
    return null;
  }

  const dbAt0 = hardwareVolumeToDb(0, cal);
  const maxVolume = getEffectiveMaxVolume(cal);
  const dbAtMax = hardwareVolumeToDb(maxVolume, cal);

  if (dbAt0 === null || dbAtMax === null) {
    return null;
  }

  return {
    minDb: Math.round(dbAt0),
    maxDb: Math.round(dbAtMax)
  };
}

/**
 * Get current dB level for a slider position
 */
export function getSliderDb(sliderPercent: number, cal: EndpointCalibration): number | null {
  if (!cal.calibrated) {
    return null;
  }

  const hardwarePercent = sliderToHardware(sliderPercent, cal);
  return hardwareVolumeToDb(hardwarePercent, cal);
}

// ============================================================================
// Test Tone API
// ============================================================================

const API_BASE = '/api/testtone';

export type ToneType = 'pink' | 'sine' | 'sweep';

export interface ToneStatus {
  status: 'playing' | 'stopped';
  type?: ToneType;
  volume?: number;
  client_id?: string;
  elapsed?: number;
  remaining?: number;
}

/**
 * Start playing a test tone on an endpoint
 */
export async function startTestTone(
  clientId: string,
  volume: number,
  type: ToneType = 'pink',
  duration: number = 60
): Promise<{status: string; error?: string}> {
  try {
    const response = await fetch(`${API_BASE}/start`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        client_id: clientId,
        volume,
        type,
        duration
      })
    });

    return await response.json();
  } catch (error) {
    console.error('Failed to start test tone:', error);
    return {status: 'error', error: String(error)};
  }
}

/**
 * Stop the currently playing test tone
 */
export async function stopTestTone(): Promise<{status: string; error?: string}> {
  try {
    const response = await fetch(`${API_BASE}/stop`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'}
    });

    return await response.json();
  } catch (error) {
    console.error('Failed to stop test tone:', error);
    return {status: 'error', error: String(error)};
  }
}

/**
 * Adjust volume while tone is playing
 */
export async function setTestToneVolume(
  clientId: string,
  volume: number
): Promise<{status: string; volume?: number; error?: string}> {
  try {
    const response = await fetch(`${API_BASE}/volume`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        client_id: clientId,
        volume
      })
    });

    return await response.json();
  } catch (error) {
    console.error('Failed to set test tone volume:', error);
    return {status: 'error', error: String(error)};
  }
}

/**
 * Get current test tone status
 */
export async function getTestToneStatus(): Promise<ToneStatus> {
  try {
    const response = await fetch(`${API_BASE}/status`);
    return await response.json();
  } catch (error) {
    console.error('Failed to get test tone status:', error);
    return {status: 'stopped'};
  }
}

// ============================================================================
// Calibration Settings Persistence
// ============================================================================

/**
 * Get calibration data for all endpoints
 */
export async function getAllCalibrations(): Promise<AudioCalibrationSettings> {
  try {
    const settings = settingsService.getMergedSettings();
    return settings.audio?.calibration || {};
  } catch (error) {
    console.error('Failed to get calibrations:', error);
    return {};
  }
}

/**
 * Get calibration data for a specific endpoint
 */
export async function getCalibration(clientId: string): Promise<EndpointCalibration> {
  const calibrations = await getAllCalibrations();
  return calibrations[clientId] || {...DEFAULT_CALIBRATION};
}

/**
 * Save calibration data for an endpoint
 */
export async function saveCalibration(
  clientId: string,
  calibration: EndpointCalibration
): Promise<boolean> {
  try {
    const settings = settingsService.getMergedSettings();

    // Ensure audio.calibration structure exists
    const updatedSettings = {
      ...settings,
      audio: {
        ...settings.audio,
        calibration: {
          ...(settings.audio?.calibration || {}),
          [clientId]: {
            ...calibration,
            lastCalibrated: new Date().toISOString()
          }
        }
      }
    };

    await settingsService.updateSettings(updatedSettings);
    return true;
  } catch (error) {
    console.error('Failed to save calibration:', error);
    return false;
  }
}

/**
 * Delete calibration data for an endpoint
 */
export async function deleteCalibration(clientId: string): Promise<boolean> {
  try {
    const settings = settingsService.getMergedSettings();

    if (settings.audio?.calibration?.[clientId]) {
      const calibration = {...settings.audio.calibration};
      delete calibration[clientId];

      const updatedSettings = {
        ...settings,
        audio: {
          ...settings.audio,
          calibration
        }
      };

      await settingsService.updateSettings(updatedSettings);
    }

    return true;
  } catch (error) {
    console.error('Failed to delete calibration:', error);
    return false;
  }
}

/**
 * Create a calibration object from wizard measurements
 */
export function createCalibration(
  name: string,
  lowRef: CalibrationReference,
  highRef: CalibrationReference,
  maxLimit: CalibrationMaxLimit,
  defaultVolume: number
): EndpointCalibration {
  return {
    name,
    calibrated: true,
    lowRef,
    highRef,
    maxLimit,
    defaultVolume,
    lastCalibrated: new Date().toISOString()
  };
}

export const calibrationService = {
  // Calculation utilities
  hardwareVolumeToDb,
  dbToHardwareVolume,
  getEffectiveMaxVolume,
  sliderToHardware,
  hardwareToSlider,
  getMatchingSliderVolume,
  getDbRange,
  getSliderDb,

  // Test tone API
  startTestTone,
  stopTestTone,
  setTestToneVolume,
  getTestToneStatus,

  // Settings persistence
  getAllCalibrations,
  getCalibration,
  saveCalibration,
  deleteCalibration,
  createCalibration,

  // Constants
  DEFAULT_CALIBRATION,
  CALIBRATION_LOW_VOLUME,
  CALIBRATION_HIGH_VOLUME
};
