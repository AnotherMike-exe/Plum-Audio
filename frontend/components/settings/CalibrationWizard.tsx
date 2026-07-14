import React, {useState, useEffect, useCallback} from 'react';
import {Icon} from '../Icon';
import {
  calibrationService,
  CALIBRATION_LOW_VOLUME,
  CALIBRATION_HIGH_VOLUME,
  type ToneType
} from '../../services/calibrationService';
import type {EndpointCalibration, CalibrationMaxLimit} from '../../types';

interface CalibrationWizardProps {
  clientId: string;
  clientName: string;
  existingCalibration?: EndpointCalibration;
  onComplete: (calibration: EndpointCalibration) => void;
  onCancel: () => void;
}

type CalibrationMethod = 'feel' | 'db-meter';
type WizardStep = 'method' | 'low-ref' | 'high-ref' | 'configure' | 'complete';

export const CalibrationWizard: React.FC<CalibrationWizardProps> = ({
  clientId,
  clientName,
  existingCalibration,
  onComplete,
  onCancel
}) => {
  const [step, setStep] = useState<WizardStep>('method');
  const [method, setMethod] = useState<CalibrationMethod>('db-meter');

  // Measurement state
  const [lowVolume] = useState(CALIBRATION_LOW_VOLUME);
  const [highVolume] = useState(CALIBRATION_HIGH_VOLUME);
  const [lowDb, setLowDb] = useState<string>('');
  const [highDb, setHighDb] = useState<string>('');

  // Configuration state
  const [maxLimitMode, setMaxLimitMode] = useState<'percentage' | 'decibel'>(
    existingCalibration?.maxLimit?.mode || 'percentage'
  );
  const [maxLimitValue, setMaxLimitValue] = useState<number>(
    existingCalibration?.maxLimit?.value || 85
  );
  const [defaultVolume, setDefaultVolume] = useState<number>(
    existingCalibration?.defaultVolume || 80
  );

  // "By feel" mode state
  const [feelVolume, setFeelVolume] = useState<number>(75);

  // Test tone state
  const [isPlaying, setIsPlaying] = useState(false);
  const [toneType, setToneType] = useState<ToneType>('pink');
  const [currentVolume, setCurrentVolume] = useState<number>(CALIBRATION_LOW_VOLUME);

  // Stop tone on unmount
  useEffect(() => {
    return () => {
      calibrationService.stopTestTone();
    };
  }, []);

  const handlePlayTone = useCallback(async (volume: number) => {
    setCurrentVolume(volume);
    const result = await calibrationService.startTestTone(clientId, volume, toneType, 120);
    if (result.status === 'playing') {
      setIsPlaying(true);
    }
  }, [clientId, toneType]);

  const handleStopTone = useCallback(async () => {
    await calibrationService.stopTestTone();
    setIsPlaying(false);
  }, []);

  const handleVolumeChange = useCallback(async (volume: number) => {
    setCurrentVolume(volume);
    if (isPlaying) {
      await calibrationService.setTestToneVolume(clientId, volume);
    }
  }, [clientId, isPlaying]);

  const handleMethodSelect = (selectedMethod: CalibrationMethod) => {
    setMethod(selectedMethod);
    if (selectedMethod === 'feel') {
      setStep('configure');
    } else {
      setStep('low-ref');
    }
  };

  const handleLowRefNext = () => {
    if (!lowDb || isNaN(parseFloat(lowDb))) {
      return;
    }
    handleStopTone();
    setStep('high-ref');
  };

  const handleHighRefNext = () => {
    if (!highDb || isNaN(parseFloat(highDb))) {
      return;
    }
    handleStopTone();
    setStep('configure');
  };

  const handleSave = async () => {
    handleStopTone();

    let calibration: EndpointCalibration;

    if (method === 'db-meter') {
      calibration = calibrationService.createCalibration(
        clientName,
        {volume: lowVolume, measuredDb: parseFloat(lowDb)},
        {volume: highVolume, measuredDb: parseFloat(highDb)},
        {mode: maxLimitMode, value: maxLimitValue},
        defaultVolume
      );
    } else {
      // "By feel" mode - create uncalibrated config with just defaults
      calibration = {
        name: clientName,
        calibrated: false,
        maxLimit: {mode: 'percentage', value: 100},
        defaultVolume: feelVolume
      };
    }

    const saved = await calibrationService.saveCalibration(clientId, calibration);
    if (saved) {
      onComplete(calibration);
    }
  };

  // Calculate estimated dB values for preview
  const getEstimatedDb = (volume: number): string => {
    if (method !== 'db-meter' || !lowDb || !highDb) return '';

    const lowDbNum = parseFloat(lowDb);
    const highDbNum = parseFloat(highDb);
    if (isNaN(lowDbNum) || isNaN(highDbNum)) return '';

    const slope = (highDbNum - lowDbNum) / (highVolume - lowVolume);
    const intercept = lowDbNum - (slope * lowVolume);
    const estimatedDb = slope * volume + intercept;

    return `${Math.round(estimatedDb)}`;
  };

  const renderMethodStep = () => (
    <div className="space-y-4">
      <div className="text-center mb-6">
        <h3 className="text-lg font-semibold text-[var(--text-primary)] mb-2">
          Calibrate: {clientName}
        </h3>
        <p className="text-sm text-[var(--text-secondary)]">
          Choose how you want to calibrate this endpoint
        </p>
      </div>

      <div className="space-y-3">
        <button
          onClick={() => handleMethodSelect('db-meter')}
          className={`w-full p-4 rounded-lg border text-left transition-all ${
            method === 'db-meter'
              ? 'bg-[var(--accent-color)]/10 border-[var(--accent-color)]'
              : 'bg-[var(--bg-tertiary)] border-[var(--border-color)] hover:border-[var(--text-secondary)]'
          }`}
        >
          <div className="flex items-start gap-3">
            <Icon name="gauge" className="text-xl text-[var(--accent-color)] mt-0.5" />
            <div>
              <div className="font-medium text-[var(--text-primary)]">With dB Meter</div>
              <div className="text-sm text-[var(--text-secondary)] mt-1">
                Use a phone app for precise measurements. Enables dB-matched volume across rooms.
              </div>
              <div className="text-xs text-[var(--text-secondary)] mt-2">
                Recommended apps: NIOSH SLM (iOS), Sound Meter (Android)
              </div>
            </div>
          </div>
        </button>

        <button
          onClick={() => handleMethodSelect('feel')}
          className={`w-full p-4 rounded-lg border text-left transition-all ${
            method === 'feel'
              ? 'bg-[var(--accent-color)]/10 border-[var(--accent-color)]'
              : 'bg-[var(--bg-tertiary)] border-[var(--border-color)] hover:border-[var(--text-secondary)]'
          }`}
        >
          <div className="flex items-start gap-3">
            <Icon name="sliders" className="text-xl text-[var(--text-secondary)] mt-0.5" />
            <div>
              <div className="font-medium text-[var(--text-primary)]">By Feel</div>
              <div className="text-sm text-[var(--text-secondary)] mt-1">
                Adjust until it sounds right - no tools needed. Sets a default volume only.
              </div>
            </div>
          </div>
        </button>
      </div>

      <div className="flex justify-end gap-2 pt-4">
        <button
          onClick={onCancel}
          className="px-4 py-2 text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
        >
          Cancel
        </button>
      </div>
    </div>
  );

  const renderMeasurementStep = (
    title: string,
    volume: number,
    dbValue: string,
    setDbValue: (val: string) => void,
    onNext: () => void,
    onBack: () => void,
    stepNumber: number
  ) => (
    <div className="space-y-4">
      <div className="text-center mb-4">
        <h3 className="text-lg font-semibold text-[var(--text-primary)] mb-1">
          Step {stepNumber} of 3: {title}
        </h3>
        <p className="text-sm text-[var(--text-secondary)]">
          Playing test tone at {volume}% volume
        </p>
      </div>

      {/* Volume indicator */}
      <div className="flex items-center gap-3 p-3 bg-[var(--bg-tertiary)] rounded-lg">
        <Icon name="volume-high" className="text-[var(--accent-color)]" />
        <div className="flex-1 h-3 bg-[var(--bg-primary)] rounded-full overflow-hidden">
          <div
            className="h-full bg-[var(--accent-color)] transition-all duration-300"
            style={{width: `${volume}%`}}
          />
        </div>
        <span className="text-sm font-medium text-[var(--text-primary)] w-12 text-right">
          {volume}%
        </span>
      </div>

      {/* Play/Stop button */}
      <div className="flex justify-center">
        {isPlaying ? (
          <button
            onClick={handleStopTone}
            className="flex items-center gap-2 px-4 py-2 bg-red-500/20 text-red-400 rounded-lg hover:bg-red-500/30"
          >
            <Icon name="stop" />
            <span>Stop Tone</span>
          </button>
        ) : (
          <button
            onClick={() => handlePlayTone(volume)}
            className="flex items-center gap-2 px-4 py-2 bg-[var(--accent-color)] accent-button-text rounded-lg hover:opacity-90"
          >
            <Icon name="play" />
            <span>Play Test Tone</span>
          </button>
        )}
      </div>

      {/* Tone type selector */}
      <div className="flex items-center justify-center gap-2">
        <span className="text-xs text-[var(--text-secondary)]">Tone:</span>
        <select
          value={toneType}
          onChange={(e) => setToneType(e.target.value as ToneType)}
          className="text-xs px-2 py-1 bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded text-[var(--text-primary)]"
        >
          <option value="pink">Pink Noise</option>
          <option value="sine">1kHz Sine</option>
          <option value="sweep">Frequency Sweep</option>
        </select>
      </div>

      {/* Instructions */}
      <div className="p-3 bg-blue-500/10 border border-blue-500/20 rounded-lg">
        <div className="flex items-start gap-2">
          <Icon name="circle-info" className="text-blue-400 mt-0.5 flex-shrink-0" />
          <div className="text-sm text-blue-300">
            <p className="font-medium mb-1">Instructions:</p>
            <ol className="list-decimal list-inside space-y-1 text-blue-300/80">
              <li>Open your dB meter app on your phone</li>
              <li>Stand where you normally listen</li>
              <li>Wait for the reading to stabilize</li>
              <li>Enter the dB level shown below</li>
            </ol>
          </div>
        </div>
      </div>

      {/* dB input */}
      <div className="flex items-center gap-2">
        <label className="text-sm text-[var(--text-secondary)]">Measured Level:</label>
        <input
          type="number"
          value={dbValue}
          onChange={(e) => setDbValue(e.target.value)}
          placeholder="e.g., 55"
          className="flex-1 px-3 py-2 bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded text-[var(--text-primary)] focus:outline-none focus:ring-2 focus:ring-[var(--accent-color)]"
        />
        <span className="text-sm text-[var(--text-secondary)]">dB</span>
      </div>

      {/* Navigation */}
      <div className="flex justify-between pt-4">
        <button
          onClick={() => {
            handleStopTone();
            onBack();
          }}
          className="px-4 py-2 text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
        >
          Back
        </button>
        <button
          onClick={onNext}
          disabled={!dbValue || isNaN(parseFloat(dbValue))}
          className="px-4 py-2 bg-[var(--accent-color)] accent-button-text rounded-lg hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          Next
        </button>
      </div>
    </div>
  );

  const renderConfigureStep = () => {
    const estimatedDbAt100 = method === 'db-meter' ? getEstimatedDb(100) : '';
    const estimatedDbAtMax = method === 'db-meter' ? getEstimatedDb(maxLimitMode === 'percentage' ? maxLimitValue : 100) : '';
    const estimatedDbAtDefault = method === 'db-meter' ? getEstimatedDb(defaultVolume * (maxLimitMode === 'percentage' ? maxLimitValue : 100) / 100) : '';

    return (
      <div className="space-y-4">
        <div className="text-center mb-4">
          <h3 className="text-lg font-semibold text-[var(--text-primary)] mb-1">
            {method === 'db-meter' ? 'Step 3 of 3: Configure Limits' : 'Set Default Volume'}
          </h3>
          {method === 'db-meter' && (
            <p className="text-sm text-[var(--text-secondary)]">
              Based on your measurements: {lowVolume}% = {lowDb} dB, {highVolume}% = {highDb} dB
            </p>
          )}
        </div>

        {method === 'feel' ? (
          /* By Feel mode - just set default volume */
          <div className="space-y-4">
            <p className="text-sm text-[var(--text-secondary)]">
              Adjust the slider until the volume is comfortable for normal listening.
            </p>

            <div className="flex items-center gap-3">
              <Icon name="volume-low" className="text-[var(--text-secondary)]" />
              <input
                type="range"
                min="0"
                max="100"
                value={feelVolume}
                onChange={(e) => {
                  const vol = parseInt(e.target.value);
                  setFeelVolume(vol);
                  handleVolumeChange(vol);
                }}
                className="flex-1"
              />
              <Icon name="volume-high" className="text-[var(--text-secondary)]" />
              <span className="text-sm font-medium text-[var(--text-primary)] w-12 text-right">
                {feelVolume}%
              </span>
            </div>

            <div className="flex justify-center">
              {isPlaying ? (
                <button
                  onClick={handleStopTone}
                  className="flex items-center gap-2 px-4 py-2 bg-red-500/20 text-red-400 rounded-lg hover:bg-red-500/30"
                >
                  <Icon name="stop" />
                  <span>Stop Tone</span>
                </button>
              ) : (
                <button
                  onClick={() => handlePlayTone(feelVolume)}
                  className="flex items-center gap-2 px-4 py-2 bg-[var(--accent-color)] accent-button-text rounded-lg hover:opacity-90"
                >
                  <Icon name="play" />
                  <span>Play Test Tone</span>
                </button>
              )}
            </div>

            <div className="p-3 bg-yellow-500/10 border border-yellow-500/20 rounded-lg">
              <div className="flex items-start gap-2">
                <Icon name="circle-info" className="text-yellow-400 mt-0.5 flex-shrink-0" />
                <p className="text-sm text-yellow-300">
                  This sets a default volume only. For dB-matched multi-room audio, use the "With dB Meter" option.
                </p>
              </div>
            </div>
          </div>
        ) : (
          /* dB Meter mode - full configuration */
          <div className="space-y-5">
            {/* Maximum Output Limit */}
            <div className="space-y-3">
              <label className="block text-sm font-medium text-[var(--text-primary)]">
                Maximum Output Limit
              </label>

              <div className="flex gap-2">
                <button
                  onClick={() => setMaxLimitMode('percentage')}
                  className={`flex-1 px-3 py-2 rounded-lg text-sm ${
                    maxLimitMode === 'percentage'
                      ? 'bg-[var(--accent-color)] accent-button-text'
                      : 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:bg-[var(--bg-primary)]'
                  }`}
                >
                  By Percentage
                </button>
                <button
                  onClick={() => setMaxLimitMode('decibel')}
                  className={`flex-1 px-3 py-2 rounded-lg text-sm ${
                    maxLimitMode === 'decibel'
                      ? 'bg-[var(--accent-color)] accent-button-text'
                      : 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:bg-[var(--bg-primary)]'
                  }`}
                >
                  By Decibel
                </button>
              </div>

              <div className="flex items-center gap-3">
                <input
                  type="range"
                  min={maxLimitMode === 'percentage' ? 10 : parseInt(lowDb) || 30}
                  max={maxLimitMode === 'percentage' ? 100 : parseInt(getEstimatedDb(100)) || 90}
                  value={maxLimitValue}
                  onChange={(e) => setMaxLimitValue(parseInt(e.target.value))}
                  className="flex-1"
                />
                <span className="text-sm font-medium text-[var(--text-primary)] w-16 text-right">
                  {maxLimitValue}{maxLimitMode === 'decibel' ? ' dB' : '%'}
                </span>
              </div>

              {maxLimitMode === 'percentage' && estimatedDbAtMax && (
                <p className="text-xs text-[var(--text-secondary)]">
                  Hardware capped at {maxLimitValue}% ({estimatedDbAtMax} dB estimated)
                </p>
              )}
              {maxLimitMode === 'decibel' && (
                <p className="text-xs text-[var(--text-secondary)]">
                  Volume will cap to stay under {maxLimitValue} dB
                </p>
              )}
            </div>

            {/* Default Startup Level */}
            <div className="space-y-3">
              <label className="block text-sm font-medium text-[var(--text-primary)]">
                Default Startup Level
              </label>

              <div className="flex items-center gap-3">
                <span className="text-xs text-[var(--text-secondary)]">Quiet</span>
                <input
                  type="range"
                  min="0"
                  max="100"
                  value={defaultVolume}
                  onChange={(e) => setDefaultVolume(parseInt(e.target.value))}
                  className="flex-1"
                />
                <span className="text-xs text-[var(--text-secondary)]">Loud</span>
                <span className="text-sm font-medium text-[var(--text-primary)] w-12 text-right">
                  {defaultVolume}%
                </span>
              </div>

              {estimatedDbAtDefault && (
                <p className="text-xs text-[var(--text-secondary)]">
                  Approximately {estimatedDbAtDefault} dB at startup
                </p>
              )}
            </div>

            {/* Preview button */}
            <div className="flex justify-center">
              <button
                onClick={() => handlePlayTone(Math.round(defaultVolume * (maxLimitMode === 'percentage' ? maxLimitValue : 100) / 100))}
                className="flex items-center gap-2 px-4 py-2 bg-[var(--bg-tertiary)] text-[var(--text-primary)] rounded-lg hover:bg-[var(--bg-primary)]"
              >
                <Icon name="play" />
                <span>Preview Default Level</span>
              </button>
            </div>

            <div className="p-3 bg-blue-500/10 border border-blue-500/20 rounded-lg">
              <div className="flex items-start gap-2">
                <Icon name="circle-info" className="text-blue-400 mt-0.5 flex-shrink-0" />
                <p className="text-sm text-blue-300">
                  Use dB mode for maximum to set a consistent limit across all rooms (e.g., 70 dB everywhere).
                </p>
              </div>
            </div>
          </div>
        )}

        {/* Navigation */}
        <div className="flex justify-between pt-4">
          <button
            onClick={() => {
              handleStopTone();
              setStep(method === 'feel' ? 'method' : 'high-ref');
            }}
            className="px-4 py-2 text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
          >
            Back
          </button>
          <button
            onClick={handleSave}
            className="px-4 py-2 bg-[var(--accent-color)] accent-button-text rounded-lg hover:opacity-90"
          >
            Save Calibration
          </button>
        </div>
      </div>
    );
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-[var(--bg-secondary)] rounded-xl shadow-xl max-w-md w-full mx-4 p-6 max-h-[90vh] overflow-y-auto">
        {step === 'method' && renderMethodStep()}
        {step === 'low-ref' && renderMeasurementStep(
          'Measure Low Reference',
          lowVolume,
          lowDb,
          setLowDb,
          handleLowRefNext,
          () => setStep('method'),
          1
        )}
        {step === 'high-ref' && renderMeasurementStep(
          'Measure High Reference',
          highVolume,
          highDb,
          setHighDb,
          handleHighRefNext,
          () => setStep('low-ref'),
          2
        )}
        {step === 'configure' && renderConfigureStep()}
      </div>
    </div>
  );
};
