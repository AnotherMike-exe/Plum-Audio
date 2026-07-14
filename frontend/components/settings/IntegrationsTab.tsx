import React, {useState, useEffect} from 'react';
import type {Settings as SettingsType} from '../../types';
import {Switch} from '../Switch';
import {airplayService, bluetoothService, spotifyService, dlnaService, plexampService} from '../../services/integrationsService';
import { Icon } from '../Icon';

interface IntegrationsTabProps {
  settings: SettingsType;
  onSettingsChange: (newSettings: SettingsType) => void;
}

type ApplyStatus = 'idle' | 'pending' | 'applying' | 'success' | 'error';

type AirPlayEndpoint = {
  id: string;
  enabled: boolean;
  deviceName: string;
  port: number;
  udpPortBase: number;
};

type SpotifyEndpoint = {
  id: string;
  enabled: boolean;
  deviceName: string;
  zeroconfPort: number;
};

export const IntegrationsTab: React.FC<IntegrationsTabProps> = ({
  settings,
  onSettingsChange,
}) => {
  const [expandedSection, setExpandedSection] = useState<string | null>(null);

  // AirPlay endpoints state
  const [airplayEndpoints, setAirplayEndpoints] = useState<AirPlayEndpoint[]>([]);
  const [endpointNames, setEndpointNames] = useState<Record<string, string>>({});
  const [endpointNameStatuses, setEndpointNameStatuses] = useState<Record<string, ApplyStatus>>({});
  const [endpointNameMessages, setEndpointNameMessages] = useState<Record<string, string>>({});
  const [isTogglingEndpoint, setIsTogglingEndpoint] = useState<Record<string, boolean>>({});
  const [isAddingEndpoint, setIsAddingEndpoint] = useState(false);
  const [newEndpointName, setNewEndpointName] = useState('');
  const [showAddEndpoint, setShowAddEndpoint] = useState(false);

  // Spotify endpoints state
  const [spotifyEndpoints, setSpotifyEndpoints] = useState<SpotifyEndpoint[]>([]);
  const [spotifyEndpointNames, setSpotifyEndpointNames] = useState<Record<string, string>>({});
  const [spotifyEndpointNameStatuses, setSpotifyEndpointNameStatuses] = useState<Record<string, ApplyStatus>>({});
  const [spotifyEndpointNameMessages, setSpotifyEndpointNameMessages] = useState<Record<string, string>>({});
  const [isTogglingSpotifyEndpoint, setIsTogglingSpotifyEndpoint] = useState<Record<string, boolean>>({});
  const [isAddingSpotifyEndpoint, setIsAddingSpotifyEndpoint] = useState(false);
  const [newSpotifyEndpointName, setNewSpotifyEndpointName] = useState('');
  const [showAddSpotifyEndpoint, setShowAddSpotifyEndpoint] = useState(false);

  // Bluetooth device name state
  const [bluetoothDeviceName, setBluetoothDeviceName] = useState(settings.integrations.bluetooth.deviceName);
  const [bluetoothNameStatus, setBluetoothNameStatus] = useState<ApplyStatus>('idle');
  const [bluetoothNameMessage, setBluetoothNameMessage] = useState('');
  const [isTogglingBluetooth, setIsTogglingBluetooth] = useState(false);

  // DLNA endpoints state
  const [dlnaEndpoints, setDlnaEndpoints] = useState<any[]>([]);
  const [dlnaEndpointNames, setDlnaEndpointNames] = useState<Record<string, string>>({});
  const [dlnaEndpointNameStatuses, setDlnaEndpointNameStatuses] = useState<Record<string, ApplyStatus>>({});
  const [dlnaEndpointNameMessages, setDlnaEndpointNameMessages] = useState<Record<string, string>>({});
  const [isTogglingDlnaEndpoint, setIsTogglingDlnaEndpoint] = useState<Record<string, boolean>>({});
  const [isAddingDlnaEndpoint, setIsAddingDlnaEndpoint] = useState(false);
  const [newDlnaEndpointName, setNewDlnaEndpointName] = useState('');
  const [showAddDlnaEndpoint, setShowAddDlnaEndpoint] = useState(false);
  const [loadingDlnaEndpoints, setLoadingDlnaEndpoints] = useState(true);

  // Plexamp state
  const [isTogglingPlexamp, setIsTogglingPlexamp] = useState(false);

  // Load AirPlay endpoints on mount
  useEffect(() => {
    const loadEndpoints = async () => {
      try {
        const result = await airplayService.listEndpoints();
        if (result.success && result.endpoints) {
          setAirplayEndpoints(result.endpoints);
          // Initialize endpoint name states
          const names: Record<string, string> = {};
          result.endpoints.forEach(ep => {
            names[ep.id] = ep.deviceName;
          });
          setEndpointNames(names);
        }
      } catch (error) {
        console.error('Failed to load AirPlay endpoints:', error);
      }
    };
    loadEndpoints();
  }, []);

  // Load Spotify endpoints on mount
  useEffect(() => {
    const loadSpotifyEndpoints = async () => {
      try {
        const result = await spotifyService.listEndpoints();
        if (result.success && result.endpoints) {
          setSpotifyEndpoints(result.endpoints);
          // Initialize endpoint name states
          const names: Record<string, string> = {};
          result.endpoints.forEach(ep => {
            names[ep.id] = ep.deviceName;
          });
          setSpotifyEndpointNames(names);
        }
      } catch (error) {
        console.error('Failed to load Spotify endpoints:', error);
      }
    };
    loadSpotifyEndpoints();
  }, []);

  // Load DLNA endpoints on mount
  useEffect(() => {
    const loadDlnaEndpoints = async () => {
      try {
        const result = await dlnaService.listEndpoints();
        if (result.success && result.endpoints) {
          setDlnaEndpoints(result.endpoints);
          // Initialize endpoint name states
          const names: Record<string, string> = {};
          result.endpoints.forEach((ep: any) => {
            names[ep.id] = ep.deviceName;
          });
          setDlnaEndpointNames(names);
        }
      } catch (error) {
        console.error('Failed to load DLNA endpoints:', error);
      } finally {
        setLoadingDlnaEndpoints(false);
      }
    };
    loadDlnaEndpoints();
  }, []);

  useEffect(() => {
    setBluetoothDeviceName(settings.integrations.bluetooth.deviceName);
  }, [settings.integrations.bluetooth.deviceName]);

  // Check if device name has changed
  const bluetoothNameChanged = bluetoothDeviceName !== settings.integrations.bluetooth.deviceName;

  const toggleSection = (section: string) => {
    setExpandedSection(expandedSection === section ? null : section);
  };

  // AirPlay endpoint handlers
  const handleEndpointToggle = async (endpointId: string, enabled: boolean) => {
    setIsTogglingEndpoint({...isTogglingEndpoint, [endpointId]: true});
    try {
      const result = await airplayService.updateEndpoint(endpointId, undefined, enabled);

      if (result.success) {
        // Update local state
        setAirplayEndpoints(airplayEndpoints.map(ep =>
          ep.id === endpointId ? {...ep, enabled} : ep
        ));
      } else {
        alert(`Failed to ${enabled ? 'enable' : 'disable'} endpoint: ${result.message}`);
      }
    } catch (error: any) {
      alert(`Error ${enabled ? 'enabling' : 'disabling'} endpoint: ${error.message}`);
    } finally {
      setIsTogglingEndpoint({...isTogglingEndpoint, [endpointId]: false});
    }
  };

  const handleEndpointNameChange = async (endpointId: string) => {
    const newName = endpointNames[endpointId];
    const endpoint = airplayEndpoints.find(ep => ep.id === endpointId);

    if (!endpoint || newName === endpoint.deviceName) return;

    setEndpointNameStatuses({...endpointNameStatuses, [endpointId]: 'applying'});
    setEndpointNameMessages({...endpointNameMessages, [endpointId]: 'Applying...'});

    try {
      const result = await airplayService.updateEndpoint(endpointId, newName, undefined);

      if (result.success) {
        setEndpointNameStatuses({...endpointNameStatuses, [endpointId]: 'success'});
        setEndpointNameMessages({...endpointNameMessages, [endpointId]: 'Applied'});

        // Update local state
        setAirplayEndpoints(airplayEndpoints.map(ep =>
          ep.id === endpointId ? {...ep, deviceName: newName} : ep
        ));

        setTimeout(() => {
          setEndpointNameStatuses({...endpointNameStatuses, [endpointId]: 'idle'});
          setEndpointNameMessages({...endpointNameMessages, [endpointId]: ''});
        }, 3000);
      } else {
        setEndpointNameStatuses({...endpointNameStatuses, [endpointId]: 'error'});
        setEndpointNameMessages({...endpointNameMessages, [endpointId]: result.message || 'Failed'});
      }
    } catch (error: any) {
      setEndpointNameStatuses({...endpointNameStatuses, [endpointId]: 'error'});
      setEndpointNameMessages({...endpointNameMessages, [endpointId]: error.message || 'Error'});
    }
  };

  const handleAddEndpoint = async () => {
    if (!newEndpointName.trim()) {
      alert('Please enter a device name');
      return;
    }

    setIsAddingEndpoint(true);
    try {
      const result = await airplayService.addEndpoint(newEndpointName, true);

      if (result.success && result.endpoint) {
        // Update local state
        setAirplayEndpoints([...airplayEndpoints, result.endpoint]);
        setEndpointNames({...endpointNames, [result.endpoint.id]: result.endpoint.deviceName});
        setNewEndpointName('');
        setShowAddEndpoint(false);
      } else {
        alert(`Failed to add endpoint: ${result.message}`);
      }
    } catch (error: any) {
      alert(`Error adding endpoint: ${error.message}`);
    } finally {
      setIsAddingEndpoint(false);
    }
  };

  const handleRemoveEndpoint = async (endpointId: string) => {
    if (airplayEndpoints.length <= 1) {
      alert('Cannot remove the last AirPlay endpoint');
      return;
    }

    const endpoint = airplayEndpoints.find(ep => ep.id === endpointId);
    if (!endpoint) return;

    if (!confirm(`Remove AirPlay endpoint "${endpoint.deviceName}"?`)) {
      return;
    }

    try {
      const result = await airplayService.removeEndpoint(endpointId);

      if (result.success) {
        // Update local state
        setAirplayEndpoints(airplayEndpoints.filter(ep => ep.id !== endpointId));
        const newNames = {...endpointNames};
        delete newNames[endpointId];
        setEndpointNames(newNames);
      } else {
        alert(`Failed to remove endpoint: ${result.message}`);
      }
    } catch (error: any) {
      alert(`Error removing endpoint: ${error.message}`);
    }
  };

  // Spotify endpoint handlers
  const handleSpotifyEndpointToggle = async (endpointId: string, enabled: boolean) => {
    setIsTogglingSpotifyEndpoint({...isTogglingSpotifyEndpoint, [endpointId]: true});
    try {
      const result = await spotifyService.updateEndpoint(endpointId, undefined, enabled);

      if (result.success) {
        // Update local state
        setSpotifyEndpoints(spotifyEndpoints.map(ep =>
          ep.id === endpointId ? {...ep, enabled} : ep
        ));
      } else {
        alert(`Failed to ${enabled ? 'enable' : 'disable'} Spotify endpoint: ${result.message}`);
      }
    } catch (error: any) {
      alert(`Error ${enabled ? 'enabling' : 'disabling'} Spotify endpoint: ${error.message}`);
    } finally {
      setIsTogglingSpotifyEndpoint({...isTogglingSpotifyEndpoint, [endpointId]: false});
    }
  };

  const handleSpotifyEndpointNameChange = async (endpointId: string) => {
    const newName = spotifyEndpointNames[endpointId];
    const endpoint = spotifyEndpoints.find(ep => ep.id === endpointId);

    if (!endpoint || newName === endpoint.deviceName) return;

    setSpotifyEndpointNameStatuses({...spotifyEndpointNameStatuses, [endpointId]: 'applying'});
    setSpotifyEndpointNameMessages({...spotifyEndpointNameMessages, [endpointId]: 'Applying...'});

    try {
      const result = await spotifyService.updateEndpoint(endpointId, newName, undefined);

      if (result.success) {
        setSpotifyEndpointNameStatuses({...spotifyEndpointNameStatuses, [endpointId]: 'success'});
        setSpotifyEndpointNameMessages({...spotifyEndpointNameMessages, [endpointId]: 'Applied'});

        // Update local state
        setSpotifyEndpoints(spotifyEndpoints.map(ep =>
          ep.id === endpointId ? {...ep, deviceName: newName} : ep
        ));

        setTimeout(() => {
          setSpotifyEndpointNameStatuses({...spotifyEndpointNameStatuses, [endpointId]: 'idle'});
          setSpotifyEndpointNameMessages({...spotifyEndpointNameMessages, [endpointId]: ''});
        }, 3000);
      } else {
        setSpotifyEndpointNameStatuses({...spotifyEndpointNameStatuses, [endpointId]: 'error'});
        setSpotifyEndpointNameMessages({...spotifyEndpointNameMessages, [endpointId]: result.message || 'Failed'});
      }
    } catch (error: any) {
      setSpotifyEndpointNameStatuses({...spotifyEndpointNameStatuses, [endpointId]: 'error'});
      setSpotifyEndpointNameMessages({...spotifyEndpointNameMessages, [endpointId]: error.message || 'Error'});
    }
  };

  const handleAddSpotifyEndpoint = async () => {
    if (!newSpotifyEndpointName.trim()) {
      alert('Please enter a device name');
      return;
    }

    setIsAddingSpotifyEndpoint(true);
    try {
      const result = await spotifyService.addEndpoint(newSpotifyEndpointName, true);

      if (result.success && result.endpoint) {
        // Update local state
        setSpotifyEndpoints([...spotifyEndpoints, result.endpoint]);
        setSpotifyEndpointNames({...spotifyEndpointNames, [result.endpoint.id]: result.endpoint.deviceName});
        setNewSpotifyEndpointName('');
        setShowAddSpotifyEndpoint(false);
      } else {
        alert(`Failed to add Spotify endpoint: ${result.message}`);
      }
    } catch (error: any) {
      alert(`Error adding Spotify endpoint: ${error.message}`);
    } finally {
      setIsAddingSpotifyEndpoint(false);
    }
  };

  const handleRemoveSpotifyEndpoint = async (endpointId: string) => {
    if (spotifyEndpoints.length <= 1) {
      alert('Cannot remove the last Spotify endpoint');
      return;
    }

    const endpoint = spotifyEndpoints.find(ep => ep.id === endpointId);
    if (!endpoint) return;

    if (!confirm(`Remove Spotify endpoint "${endpoint.deviceName}"?`)) {
      return;
    }

    try {
      const result = await spotifyService.removeEndpoint(endpointId);

      if (result.success) {
        // Update local state
        setSpotifyEndpoints(spotifyEndpoints.filter(ep => ep.id !== endpointId));
        const newNames = {...spotifyEndpointNames};
        delete newNames[endpointId];
        setSpotifyEndpointNames(newNames);
      } else {
        alert(`Failed to remove Spotify endpoint: ${result.message}`);
      }
    } catch (error: any) {
      alert(`Error removing Spotify endpoint: ${error.message}`);
    }
  };

  // DLNA endpoint handlers
  const handleDlnaEndpointToggle = async (endpointId: string, enabled: boolean) => {
    setIsTogglingDlnaEndpoint({...isTogglingDlnaEndpoint, [endpointId]: true});

    try {
      const result = await dlnaService.updateEndpoint(endpointId, {enabled});

      if (result.success && result.endpoint) {
        // Update local state
        setDlnaEndpoints(dlnaEndpoints.map(ep =>
          ep.id === endpointId ? {...ep, enabled: result.endpoint.enabled} : ep
        ));
      } else {
        alert(`Failed to toggle DLNA endpoint: ${result.message}`);
      }
    } catch (error: any) {
      alert(`Error toggling DLNA endpoint: ${error.message}`);
    } finally {
      setIsTogglingDlnaEndpoint({...isTogglingDlnaEndpoint, [endpointId]: false});
    }
  };

  const handleDlnaEndpointNameChange = async (endpointId: string) => {
    const newName = dlnaEndpointNames[endpointId];
    if (!newName || !newName.trim()) {
      return;
    }

    const endpoint = dlnaEndpoints.find(ep => ep.id === endpointId);
    if (!endpoint) return;

    if (newName === endpoint.deviceName) {
      return; // No change
    }

    setDlnaEndpointNameStatuses({...dlnaEndpointNameStatuses, [endpointId]: 'applying'});
    setDlnaEndpointNameMessages({...dlnaEndpointNameMessages, [endpointId]: ''});

    try {
      const result = await dlnaService.updateEndpoint(endpointId, {deviceName: newName});

      if (result.success && result.endpoint) {
        // Update local state
        setDlnaEndpoints(dlnaEndpoints.map(ep =>
          ep.id === endpointId ? {...ep, deviceName: result.endpoint.deviceName} : ep
        ));
        setDlnaEndpointNameStatuses({...dlnaEndpointNameStatuses, [endpointId]: 'success'});
        setDlnaEndpointNameMessages({...dlnaEndpointNameMessages, [endpointId]: 'Device name updated'});

        // Clear success message after 3 seconds
        setTimeout(() => {
          setDlnaEndpointNameMessages({...dlnaEndpointNameMessages, [endpointId]: ''});
          setDlnaEndpointNameStatuses({...dlnaEndpointNameStatuses, [endpointId]: 'idle'});
        }, 3000);
      } else {
        setDlnaEndpointNameStatuses({...dlnaEndpointNameStatuses, [endpointId]: 'error'});
        setDlnaEndpointNameMessages({...dlnaEndpointNameMessages, [endpointId]: result.message || 'Update failed'});
      }
    } catch (error: any) {
      setDlnaEndpointNameStatuses({...dlnaEndpointNameStatuses, [endpointId]: 'error'});
      setDlnaEndpointNameMessages({...dlnaEndpointNameMessages, [endpointId]: error.message || 'Update failed'});
    }
  };

  const handleAddDlnaEndpoint = async () => {
    if (!newDlnaEndpointName.trim()) {
      alert('Please enter a device name');
      return;
    }

    setIsAddingDlnaEndpoint(true);
    try {
      const result = await dlnaService.addEndpoint(newDlnaEndpointName, true);

      if (result.success && result.endpoint) {
        // Update local state
        setDlnaEndpoints([...dlnaEndpoints, result.endpoint]);
        setDlnaEndpointNames({...dlnaEndpointNames, [result.endpoint.id]: result.endpoint.deviceName});
        setNewDlnaEndpointName('');
        setShowAddDlnaEndpoint(false);
      } else {
        alert(`Failed to add DLNA endpoint: ${result.message}`);
      }
    } catch (error: any) {
      alert(`Error adding DLNA endpoint: ${error.message}`);
    } finally {
      setIsAddingDlnaEndpoint(false);
    }
  };

  const handleRemoveDlnaEndpoint = async (endpointId: string) => {
    const endpoint = dlnaEndpoints.find(ep => ep.id === endpointId);
    if (!endpoint) return;

    if (!confirm(`Remove DLNA endpoint "${endpoint.deviceName}"?`)) {
      return;
    }

    try {
      const result = await dlnaService.removeEndpoint(endpointId);

      if (result.success) {
        // Update local state
        setDlnaEndpoints(dlnaEndpoints.filter(ep => ep.id !== endpointId));
        const newNames = {...dlnaEndpointNames};
        delete newNames[endpointId];
        setDlnaEndpointNames(newNames);
      } else {
        alert(`Failed to remove DLNA endpoint: ${result.message}`);
      }
    } catch (error: any) {
      alert(`Error removing DLNA endpoint: ${error.message}`);
    }
  };

  const handleBluetoothToggle = async (enabled: boolean) => {
    setIsTogglingBluetooth(true);
    try {
      const result = enabled
        ? await bluetoothService.enable()
        : await bluetoothService.disable();

      if (result.success) {
        onSettingsChange({
          ...settings,
          integrations: {
            ...settings.integrations,
            bluetooth: {
              ...settings.integrations.bluetooth,
              enabled,
            },
          },
        });
      } else {
        console.error('Failed to toggle Bluetooth:', result.message);
        alert(`Failed to ${enabled ? 'enable' : 'disable'} Bluetooth: ${result.message}`);
      }
    } catch (error) {
      console.error('Error toggling Bluetooth:', error);
      alert(`Error ${enabled ? 'enabling' : 'disabling'} Bluetooth`);
    } finally {
      setIsTogglingBluetooth(false);
    }
  };

  const handleApplyBluetoothDeviceName = async () => {
    if (!bluetoothNameChanged) return;

    // Track if Bluetooth was disabled before applying
    const wasDisabled = !settings.integrations.bluetooth.enabled;

    setBluetoothNameStatus('applying');
    setBluetoothNameMessage('Applying changes... this may take up to 60 seconds');

    try {
      const result = await bluetoothService.updateDeviceName(bluetoothDeviceName);

      if (result.success) {
        setBluetoothNameStatus('success');
        setBluetoothNameMessage('Applied');

        // Update settings - if Bluetooth was disabled, it's now enabled
        onSettingsChange({
          ...settings,
          integrations: {
            ...settings.integrations,
            bluetooth: {
              ...settings.integrations.bluetooth,
              deviceName: bluetoothDeviceName,
              enabled: wasDisabled ? true : settings.integrations.bluetooth.enabled,
            },
          },
        });

        // Clear success message after 3 seconds
        setTimeout(() => {
          setBluetoothNameStatus('idle');
          setBluetoothNameMessage('');
        }, 3000);
      } else {
        setBluetoothNameStatus('error');
        setBluetoothNameMessage(result.message || 'Failed to apply');
      }
    } catch (error: any) {
      setBluetoothNameStatus('error');
      setBluetoothNameMessage(error.message || 'Error applying changes');
    }
  };

  const handleBluetoothChange = async (field: string, value: boolean | string) => {
    // For discoverable, call API to apply immediately
    if (field === 'discoverable' && typeof value === 'boolean') {
      try {
        await bluetoothService.updateSettings({ discoverable: value });

        // Update local state after successful API call
        onSettingsChange({
          ...settings,
          integrations: {
            ...settings.integrations,
            bluetooth: {
              ...settings.integrations.bluetooth,
              [field]: value,
            },
          },
        });
      } catch (error: any) {
        console.error(`Failed to update Bluetooth ${field}:`, error);
        alert(`Failed to update ${field}: ${error.message}`);
      }
    } else {
      // For other fields (like adapter), just update local state
      onSettingsChange({
        ...settings,
        integrations: {
          ...settings.integrations,
          bluetooth: {
            ...settings.integrations.bluetooth,
            [field]: value,
          },
        },
      });
    }
  };

  const handlePlexampToggle = async (enabled: boolean) => {
    setIsTogglingPlexamp(true);
    try {
      const result = enabled
        ? await plexampService.enable()
        : await plexampService.disable();

      if (result.success) {
        onSettingsChange({
          ...settings,
          integrations: {
            ...settings.integrations,
            plexamp: {
              ...settings.integrations.plexamp,
              enabled,
            },
          },
        });
      } else {
        alert(`Failed to ${enabled ? 'enable' : 'disable'} Plexamp: ${result.message}`);
      }
    } catch (error: any) {
      alert(`Error ${enabled ? 'enabling' : 'disabling'} Plexamp: ${error.message}`);
    } finally {
      setIsTogglingPlexamp(false);
    }
  };


  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-base font-semibold text-[var(--text-primary)] mb-4">
          Audio Sources
        </h3>
        <p className="text-sm text-[var(--text-muted)] mb-6">
          Configure audio sources and their settings. Changes require service restart.
        </p>
      </div>

      <div className="space-y-4">
        {/* AirPlay */}
        <div className="p-4 bg-[var(--bg-tertiary)] rounded-lg border border-[var(--border-color)]">
          <div className="flex items-start justify-between gap-4">
            {/* Left: Icon */}
            <div className="w-12 flex justify-center items-center flex-shrink-0">
              <Icon name="apple" className="text-[2.5rem] text-[var(--text-secondary)]" style={{ color: 'inherit' }} aria-hidden />
            </div>

            {/* Middle: Title + Description */}
            <div className="flex flex-col flex-1">
              <span className="text-base font-semibold text-[var(--text-secondary)]">AirPlay</span>
              <p className="text-sm text-[var(--text-muted)] mt-1">
                AirPlay audio streaming (AirPlay 1 & 2) - {airplayEndpoints.length} endpoint{airplayEndpoints.length !== 1 ? 's' : ''}
              </p>
            </div>

            {/* Right: Chevron */}
            <div className="flex items-center flex-shrink-0">
              <button
                onClick={() => toggleSection('airplay')}
                className="text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors"
              >
                <Icon name={expandedSection === 'airplay' ? 'chevron-up' : 'chevron-down'} className="text-lg" style={{ color: 'inherit' }} />
              </button>
            </div>
          </div>

          {expandedSection === 'airplay' && (
            <div className="mt-4 ml-14 space-y-4">
              {/* Check if any endpoint is being edited */}
              {(() => {
                const isAnyEndpointBeingEdited = airplayEndpoints.some(ep =>
                  endpointNames[ep.id] !== ep.deviceName ||
                  endpointNameStatuses[ep.id] === 'applying'
                );
                return (
                  <>
              {/* Endpoints list */}
              {airplayEndpoints.map((endpoint) => {
                const nameChanged = endpointNames[endpoint.id] !== endpoint.deviceName;
                const nameEmpty = !(endpointNames[endpoint.id] || endpoint.deviceName)?.trim();
                return (
                  <div key={endpoint.id} className="p-3 bg-[var(--bg-secondary)] rounded border border-[var(--border-color)]">
                    <div className="flex items-center justify-between gap-3 mb-2">
                      <span className="text-sm font-medium text-[var(--text-secondary)]">
                        Endpoint #{endpoint.id}
                      </span>
                      <div className="flex items-center gap-2">
                        <div className="relative">
                          <input
                            type="checkbox"
                            className="sr-only"
                            checked={endpoint.enabled}
                            onChange={(e) => handleEndpointToggle(endpoint.id, e.target.checked)}
                            disabled={isTogglingEndpoint[endpoint.id]}
                            id={`endpoint-${endpoint.id}-toggle`}
                          />
                          <label
                            htmlFor={`endpoint-${endpoint.id}-toggle`}
                            className={`block w-10 h-5 rounded-full transition cursor-pointer ${endpoint.enabled ? 'bg-[var(--accent-color)]' : 'bg-[var(--bg-tertiary-hover)]'} ${isTogglingEndpoint[endpoint.id] ? 'opacity-50 cursor-not-allowed' : ''}`}
                          >
                            <div className={`dot absolute left-0.5 top-0.5 bg-white w-4 h-4 rounded-full transition-transform ${endpoint.enabled ? 'translate-x-5' : ''}`}></div>
                          </label>
                        </div>
                        {airplayEndpoints.length > 1 && (
                          <button
                            onClick={() => handleRemoveEndpoint(endpoint.id)}
                            className="text-red-500 hover:text-red-400 text-xs"
                            title="Remove endpoint"
                          >
                            <Icon name="trash" className="text-sm" style={{ color: 'inherit' }} />
                          </button>
                        )}
                      </div>
                    </div>
                    <div>
                      <label className="block text-xs text-[var(--text-muted)] mb-1">
                        Device Name
                      </label>
                      <div className="relative">
                        <input
                          type="text"
                          value={endpointNames[endpoint.id] || endpoint.deviceName}
                          onChange={(e) => setEndpointNames({...endpointNames, [endpoint.id]: e.target.value})}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' && nameChanged && !nameEmpty) {
                              handleEndpointNameChange(endpoint.id);
                            }
                          }}
                          className="w-full px-2 py-1.5 pr-16 bg-[var(--bg-primary)] border border-[var(--border-color)] rounded text-sm text-[var(--text-primary)] focus:outline-none focus:ring-2 focus:ring-[var(--accent-color)]"
                          placeholder="Plum Audio"
                          disabled={endpointNameStatuses[endpoint.id] === 'applying'}
                        />
                        {nameChanged && (
                          <button
                            onClick={() => handleEndpointNameChange(endpoint.id)}
                            disabled={nameEmpty || endpointNameStatuses[endpoint.id] === 'applying'}
                            className="absolute right-1.5 top-1/2 -translate-y-1/2 px-2 py-0.5 text-xs bg-[var(--accent-color)] accent-button-text rounded hover:opacity-80 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {endpointNameStatuses[endpoint.id] === 'applying' ? 'Applying...' : 'Apply'}
                          </button>
                        )}
                      </div>
                      {endpointNameMessages[endpoint.id] && (
                        <p className={`text-xs mt-1 ${
                          endpointNameStatuses[endpoint.id] === 'success'
                            ? 'text-green-500'
                            : endpointNameStatuses[endpoint.id] === 'error'
                            ? 'text-red-500'
                            : 'text-[var(--text-muted)]'
                        }`}>
                          {endpointNameMessages[endpoint.id]}
                        </p>
                      )}
                      {nameChanged && !endpointNameMessages[endpoint.id] && (
                        <p className="text-xs text-amber-500 mt-1">
                          Pending changes
                        </p>
                      )}
                    </div>
                    <div className="text-xs text-[var(--text-muted)] mt-2">
                      Port: {endpoint.port}, UDP: {endpoint.udpPortBase}-{endpoint.udpPortBase + 9}
                    </div>
                  </div>
                );
              })}

              {/* Add endpoint button/form */}
              {!showAddEndpoint && airplayEndpoints.length < 10 && (
                <button
                  onClick={() => setShowAddEndpoint(true)}
                  disabled={isAnyEndpointBeingEdited}
                  className="w-full px-3 py-2 bg-[var(--bg-secondary)] hover:bg-[var(--bg-tertiary-hover)] border border-[var(--border-color)] rounded text-sm text-[var(--text-secondary)] transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  + Add Endpoint
                </button>
              )}

              {showAddEndpoint && (
                <div className="p-3 bg-[var(--bg-secondary)] rounded border border-[var(--accent-color)]">
                  <label className="block text-xs text-[var(--text-secondary)] mb-1">
                    New Endpoint Name
                  </label>
                  <div className="flex gap-2">
                    <input
                      type="text"
                      value={newEndpointName}
                      onChange={(e) => setNewEndpointName(e.target.value)}
                      onKeyDown={(e) => e.key === 'Enter' && newEndpointName.trim() && !isAnyEndpointBeingEdited && handleAddEndpoint()}
                      className="flex-1 px-2 py-1.5 bg-[var(--bg-primary)] border border-[var(--border-color)] rounded text-sm text-[var(--text-primary)] focus:outline-none focus:ring-2 focus:ring-[var(--accent-color)]"
                      placeholder="Living Room"
                      disabled={isAnyEndpointBeingEdited || isAddingEndpoint}
                    />
                    <button
                      onClick={handleAddEndpoint}
                      disabled={!newEndpointName.trim() || isAnyEndpointBeingEdited || isAddingEndpoint}
                      className="px-3 py-1.5 bg-[var(--accent-color)] accent-button-text rounded text-xs hover:opacity-80 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      {isAddingEndpoint ? 'Adding...' : 'Add'}
                    </button>
                    <button
                      onClick={() => {setShowAddEndpoint(false); setNewEndpointName('');}}
                      disabled={isAddingEndpoint}
                      className="px-3 py-1.5 bg-[var(--bg-tertiary)] hover:bg-[var(--bg-tertiary-hover)] rounded text-xs text-[var(--text-secondary)] disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}

              {airplayEndpoints.length >= 10 && (
                <p className="text-xs text-[var(--text-muted)] italic">
                  Maximum of 10 endpoints reached
                </p>
              )}
                  </>
                );
              })()}
            </div>
          )}
        </div>

        {/* Bluetooth */}
        <div className="p-4 bg-[var(--bg-tertiary)] rounded-lg border border-[var(--border-color)]">
          <div className="flex items-start justify-between gap-4">
            {/* Left: Icon */}
            <div className="w-12 flex justify-center items-center flex-shrink-0">
              <Icon name="bluetooth" className="text-[2.5rem] text-[var(--text-secondary)]" style={{ color: 'inherit' }} aria-hidden />
            </div>

            {/* Middle: Title + Description */}
            <div className="flex flex-col flex-1">
              <span className="text-base font-semibold text-[var(--text-secondary)]">Bluetooth</span>
              <p className="text-sm text-[var(--text-muted)] mt-1">
                Bluetooth A2DP audio streaming
              </p>
              {isTogglingBluetooth && (
                <p className="text-xs text-amber-500 mt-1">
                  Processing... this may take up to 60 seconds
                </p>
              )}
            </div>

            {/* Right: Toggle + Chevron */}
            <div className="flex items-center gap-3 flex-shrink-0">
              <div className="relative">
                <input
                  type="checkbox"
                  className="sr-only"
                  checked={settings.integrations.bluetooth.enabled}
                  onChange={(e) => handleBluetoothToggle(e.target.checked)}
                  disabled={isTogglingBluetooth}
                  id="bluetooth-toggle"
                />
                <label
                  htmlFor="bluetooth-toggle"
                  className={`block w-12 h-6 rounded-full transition cursor-pointer ${settings.integrations.bluetooth.enabled ? 'bg-[var(--accent-color)]' : 'bg-[var(--bg-tertiary-hover)]'} ${isTogglingBluetooth ? 'opacity-50 cursor-not-allowed' : ''}`}
                >
                  <div className={`dot absolute left-1 top-1 bg-white w-4 h-4 rounded-full transition-transform ${settings.integrations.bluetooth.enabled ? 'translate-x-6' : ''}`}></div>
                </label>
              </div>
              <button
                onClick={() => toggleSection('bluetooth')}
                className="text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors"
              >
                <Icon name={expandedSection === 'bluetooth' ? 'chevron-up' : 'chevron-down'} className="text-lg" style={{ color: 'inherit' }} />
              </button>
            </div>
          </div>

          {expandedSection === 'bluetooth' && (
            <div className="mt-4 ml-14 space-y-3">
              <div>
                <label className="block text-sm text-[var(--text-secondary)] mb-1">
                  Device Name
                </label>
                <div className="relative">
                  <input
                    type="text"
                    value={bluetoothDeviceName}
                    onChange={(e) => setBluetoothDeviceName(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && handleApplyBluetoothDeviceName()}
                    disabled={bluetoothNameStatus === 'applying'}
                    className="w-full px-3 py-2 pr-20 bg-[var(--bg-secondary)] border border-[var(--border-color)] rounded text-[var(--text-primary)] focus:outline-none focus:ring-2 focus:ring-[var(--accent-color)] disabled:opacity-50"
                    placeholder="Plum Audio"
                  />
                  {bluetoothNameChanged && (
                    <button
                      onClick={handleApplyBluetoothDeviceName}
                      disabled={bluetoothNameStatus === 'applying'}
                      className="absolute right-2 top-1/2 -translate-y-1/2 px-3 py-1 text-xs bg-[var(--accent-color)] accent-button-text rounded hover:opacity-80 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      {bluetoothNameStatus === 'applying' ? 'Applying...' : 'Apply'}
                    </button>
                  )}
                </div>
                {bluetoothNameMessage && (
                  <p className={`text-xs mt-1 ${
                    bluetoothNameStatus === 'success'
                      ? 'text-green-500'
                      : bluetoothNameStatus === 'error'
                      ? 'text-red-500'
                      : 'text-[var(--text-muted)]'
                  }`}>
                    {bluetoothNameMessage}
                  </p>
                )}
                {bluetoothNameChanged && !bluetoothNameMessage && (
                  <p className="text-xs text-amber-500 mt-1">
                    Pending changes - press Enter or click Apply
                  </p>
                )}
              </div>
              <div>
                <label className="block text-sm text-[var(--text-secondary)] mb-1">
                  Adapter
                </label>
                <input
                  type="text"
                  value={settings.integrations.bluetooth.adapter}
                  onChange={(e) => handleBluetoothChange('adapter', e.target.value)}
                  className="w-full px-3 py-2 bg-[var(--bg-secondary)] border border-[var(--border-color)] rounded text-[var(--text-primary)] focus:outline-none focus:ring-2 focus:ring-[var(--accent-color)]"
                  placeholder="hci0"
                />
              </div>
              <div className="flex items-center">
                <input
                  type="checkbox"
                  id="bt-discoverable"
                  checked={settings.integrations.bluetooth.discoverable}
                  onChange={(e) => handleBluetoothChange('discoverable', e.target.checked)}
                  className="mr-2"
                />
                <label htmlFor="bt-discoverable" className="text-sm text-[var(--text-secondary)]">
                  Always discoverable
                </label>
              </div>
            </div>
          )}
        </div>

        {/* Spotify Connect */}
        <div className="p-4 bg-[var(--bg-tertiary)] rounded-lg border border-[var(--border-color)]">
          <div className="flex items-start justify-between gap-4">
            {/* Left: Icon */}
            <div className="w-12 flex justify-center items-center flex-shrink-0">
              <Icon name="spotify" className="text-[2.5rem] text-[var(--text-secondary)]" style={{ color: 'inherit' }} aria-hidden />
            </div>

            {/* Middle: Title + Description */}
            <div className="flex flex-col flex-1">
              <span className="text-base font-semibold text-[var(--text-secondary)]">Spotify Connect</span>
              <p className="text-sm text-[var(--text-muted)] mt-1">
                Stream music directly from Spotify - {spotifyEndpoints.length} endpoint{spotifyEndpoints.length !== 1 ? 's' : ''}
              </p>
            </div>

            {/* Right: Chevron */}
            <div className="flex items-center flex-shrink-0">
              <button
                onClick={() => toggleSection('spotify')}
                className="text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors"
              >
                <Icon name={expandedSection === 'spotify' ? 'chevron-up' : 'chevron-down'} className="text-lg" style={{ color: 'inherit' }} />
              </button>
            </div>
          </div>

          {expandedSection === 'spotify' && (
            <div className="mt-4 ml-14 space-y-4">
              {/* Check if any endpoint is being edited */}
              {(() => {
                const isAnySpotifyEndpointBeingEdited = spotifyEndpoints.some(ep =>
                  spotifyEndpointNames[ep.id] !== ep.deviceName ||
                  spotifyEndpointNameStatuses[ep.id] === 'applying'
                );
                return (
                  <>
              {/* Endpoints list */}
              {spotifyEndpoints.map((endpoint) => {
                const nameChanged = spotifyEndpointNames[endpoint.id] !== endpoint.deviceName;
                const nameEmpty = !(spotifyEndpointNames[endpoint.id] || endpoint.deviceName)?.trim();
                return (
                  <div key={endpoint.id} className="p-3 bg-[var(--bg-secondary)] rounded border border-[var(--border-color)]">
                    <div className="flex items-center justify-between gap-3 mb-2">
                      <span className="text-sm font-medium text-[var(--text-secondary)]">
                        Endpoint #{endpoint.id}
                      </span>
                      <div className="flex items-center gap-2">
                        <div className="relative">
                          <input
                            type="checkbox"
                            className="sr-only"
                            checked={endpoint.enabled}
                            onChange={(e) => handleSpotifyEndpointToggle(endpoint.id, e.target.checked)}
                            disabled={isTogglingSpotifyEndpoint[endpoint.id]}
                            id={`spotify-endpoint-${endpoint.id}-toggle`}
                          />
                          <label
                            htmlFor={`spotify-endpoint-${endpoint.id}-toggle`}
                            className={`block w-10 h-5 rounded-full transition cursor-pointer ${endpoint.enabled ? 'bg-[var(--accent-color)]' : 'bg-[var(--bg-tertiary-hover)]'} ${isTogglingSpotifyEndpoint[endpoint.id] ? 'opacity-50 cursor-not-allowed' : ''}`}
                          >
                            <div className={`dot absolute left-0.5 top-0.5 bg-white w-4 h-4 rounded-full transition-transform ${endpoint.enabled ? 'translate-x-5' : ''}`}></div>
                          </label>
                        </div>
                        {spotifyEndpoints.length > 1 && (
                          <button
                            onClick={() => handleRemoveSpotifyEndpoint(endpoint.id)}
                            className="text-red-500 hover:text-red-400 text-xs"
                            title="Remove endpoint"
                          >
                            <Icon name="trash" className="text-sm" style={{ color: 'inherit' }} />
                          </button>
                        )}
                      </div>
                    </div>
                    <div>
                      <label className="block text-xs text-[var(--text-muted)] mb-1">
                        Device Name
                      </label>
                      <div className="relative">
                        <input
                          type="text"
                          value={spotifyEndpointNames[endpoint.id] || endpoint.deviceName}
                          onChange={(e) => setSpotifyEndpointNames({...spotifyEndpointNames, [endpoint.id]: e.target.value})}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' && nameChanged && !nameEmpty) {
                              handleSpotifyEndpointNameChange(endpoint.id);
                            }
                          }}
                          className="w-full px-2 py-1.5 pr-16 bg-[var(--bg-primary)] border border-[var(--border-color)] rounded text-sm text-[var(--text-primary)] focus:outline-none focus:ring-2 focus:ring-[var(--accent-color)]"
                          placeholder="Plum Audio"
                          disabled={spotifyEndpointNameStatuses[endpoint.id] === 'applying'}
                        />
                        {nameChanged && (
                          <button
                            onClick={() => handleSpotifyEndpointNameChange(endpoint.id)}
                            disabled={nameEmpty || spotifyEndpointNameStatuses[endpoint.id] === 'applying'}
                            className="absolute right-1.5 top-1/2 -translate-y-1/2 px-2 py-0.5 text-xs bg-[var(--accent-color)] accent-button-text rounded hover:opacity-80 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {spotifyEndpointNameStatuses[endpoint.id] === 'applying' ? 'Applying...' : 'Apply'}
                          </button>
                        )}
                      </div>
                      {spotifyEndpointNameMessages[endpoint.id] && (
                        <p className={`text-xs mt-1 ${
                          spotifyEndpointNameStatuses[endpoint.id] === 'success'
                            ? 'text-green-500'
                            : spotifyEndpointNameStatuses[endpoint.id] === 'error'
                            ? 'text-red-500'
                            : 'text-[var(--text-muted)]'
                        }`}>
                          {spotifyEndpointNameMessages[endpoint.id]}
                        </p>
                      )}
                      {nameChanged && !spotifyEndpointNameMessages[endpoint.id] && (
                        <p className="text-xs text-amber-500 mt-1">
                          Pending changes
                        </p>
                      )}
                    </div>
                    <div className="text-xs text-[var(--text-muted)] mt-2">
                      Zeroconf Port: {endpoint.zeroconfPort}
                    </div>
                  </div>
                );
              })}

              {/* Add endpoint button/form */}
              {!showAddSpotifyEndpoint && spotifyEndpoints.length < 10 && (
                <button
                  onClick={() => setShowAddSpotifyEndpoint(true)}
                  disabled={isAnySpotifyEndpointBeingEdited}
                  className="w-full px-3 py-2 bg-[var(--bg-secondary)] hover:bg-[var(--bg-tertiary-hover)] border border-[var(--border-color)] rounded text-sm text-[var(--text-secondary)] transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  + Add Endpoint
                </button>
              )}

              {showAddSpotifyEndpoint && (
                <div className="p-3 bg-[var(--bg-secondary)] rounded border border-[var(--accent-color)]">
                  <label className="block text-xs text-[var(--text-secondary)] mb-1">
                    New Endpoint Name
                  </label>
                  <div className="flex gap-2">
                    <input
                      type="text"
                      value={newSpotifyEndpointName}
                      onChange={(e) => setNewSpotifyEndpointName(e.target.value)}
                      onKeyDown={(e) => e.key === 'Enter' && newSpotifyEndpointName.trim() && !isAnySpotifyEndpointBeingEdited && handleAddSpotifyEndpoint()}
                      className="flex-1 px-2 py-1.5 bg-[var(--bg-primary)] border border-[var(--border-color)] rounded text-sm text-[var(--text-primary)] focus:outline-none focus:ring-2 focus:ring-[var(--accent-color)]"
                      placeholder="Living Room"
                      disabled={isAnySpotifyEndpointBeingEdited || isAddingSpotifyEndpoint}
                    />
                    <button
                      onClick={handleAddSpotifyEndpoint}
                      disabled={!newSpotifyEndpointName.trim() || isAnySpotifyEndpointBeingEdited || isAddingSpotifyEndpoint}
                      className="px-3 py-1.5 bg-[var(--accent-color)] accent-button-text rounded text-xs hover:opacity-80 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      {isAddingSpotifyEndpoint ? 'Adding...' : 'Add'}
                    </button>
                    <button
                      onClick={() => {setShowAddSpotifyEndpoint(false); setNewSpotifyEndpointName('');}}
                      disabled={isAddingSpotifyEndpoint}
                      className="px-3 py-1.5 bg-[var(--bg-tertiary)] hover:bg-[var(--bg-tertiary-hover)] rounded text-xs text-[var(--text-secondary)] disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}

              {spotifyEndpoints.length >= 10 && (
                <p className="text-xs text-[var(--text-muted)] italic">
                  Maximum of 10 endpoints reached
                </p>
              )}
                  </>
                );
              })()}
            </div>
          )}
        </div>

        {/* DLNA/UPnP */}
        <div className="p-4 bg-[var(--bg-tertiary)] rounded-lg border border-[var(--border-color)]">
          <div className="flex items-start justify-between gap-4">
            {/* Left: Icon */}
            <div className="w-12 flex justify-center items-center flex-shrink-0">
              <Icon name="network-wired" className="text-[2.5rem] text-[var(--text-secondary)]" style={{ color: 'inherit' }} aria-hidden />
            </div>

            {/* Middle: Title + Description */}
            <div className="flex flex-col flex-1">
              <span className="text-base font-semibold text-[var(--text-secondary)]">DLNA/UPnP</span>
              <p className="text-sm text-[var(--text-muted)] mt-1">
                DLNA/UPnP media renderer - {dlnaEndpoints.length} endpoint{dlnaEndpoints.length !== 1 ? 's' : ''}
              </p>
            </div>

            {/* Right: Chevron */}
            <div className="flex items-center flex-shrink-0">
              <button
                onClick={() => toggleSection('dlna')}
                className="text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors"
              >
                <Icon name={expandedSection === 'dlna' ? 'chevron-up' : 'chevron-down'} className="text-lg" style={{ color: 'inherit' }} />
              </button>
            </div>
          </div>

          {expandedSection === 'dlna' && (
            <div className="mt-4 ml-14 space-y-4">
              {/* Check if any endpoint is being edited */}
              {(() => {
                const isAnyDlnaEndpointBeingEdited = dlnaEndpoints.some(ep =>
                  dlnaEndpointNames[ep.id] !== ep.deviceName ||
                  dlnaEndpointNameStatuses[ep.id] === 'applying'
                );
                return (
                  <>
              {/* Endpoints list */}
              {dlnaEndpoints.map((endpoint) => {
                const nameChanged = dlnaEndpointNames[endpoint.id] !== endpoint.deviceName;
                const nameEmpty = !(dlnaEndpointNames[endpoint.id] || endpoint.deviceName)?.trim();
                return (
                  <div key={endpoint.id} className="p-3 bg-[var(--bg-secondary)] rounded border border-[var(--border-color)]">
                    <div className="flex items-center justify-between gap-3 mb-2">
                      <span className="text-sm font-medium text-[var(--text-secondary)]">
                        Endpoint #{endpoint.id}
                      </span>
                      <div className="flex items-center gap-2">
                        <div className="relative">
                          <input
                            type="checkbox"
                            className="sr-only"
                            checked={endpoint.enabled}
                            onChange={(e) => handleDlnaEndpointToggle(endpoint.id, e.target.checked)}
                            disabled={isTogglingDlnaEndpoint[endpoint.id]}
                            id={`dlna-endpoint-${endpoint.id}-toggle`}
                          />
                          <label
                            htmlFor={`dlna-endpoint-${endpoint.id}-toggle`}
                            className={`block w-10 h-5 rounded-full transition cursor-pointer ${endpoint.enabled ? 'bg-[var(--accent-color)]' : 'bg-[var(--bg-tertiary-hover)]'} ${isTogglingDlnaEndpoint[endpoint.id] ? 'opacity-50 cursor-not-allowed' : ''}`}
                          >
                            <div className={`dot absolute left-0.5 top-0.5 bg-white w-4 h-4 rounded-full transition-transform ${endpoint.enabled ? 'translate-x-5' : ''}`}></div>
                          </label>
                        </div>
                        <button
                          onClick={() => handleRemoveDlnaEndpoint(endpoint.id)}
                          className="text-red-500 hover:text-red-400 text-xs"
                          title="Remove endpoint"
                        >
                          <Icon name="trash" className="text-sm" style={{ color: 'inherit' }} />
                        </button>
                      </div>
                    </div>
                    <div>
                      <label className="block text-xs text-[var(--text-muted)] mb-1">
                        Device Name
                      </label>
                      <div className="relative">
                        <input
                          type="text"
                          value={dlnaEndpointNames[endpoint.id] || endpoint.deviceName}
                          onChange={(e) => setDlnaEndpointNames({...dlnaEndpointNames, [endpoint.id]: e.target.value})}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' && nameChanged && !nameEmpty) {
                              handleDlnaEndpointNameChange(endpoint.id);
                            }
                          }}
                          className="w-full px-2 py-1.5 pr-16 bg-[var(--bg-primary)] border border-[var(--border-color)] rounded text-sm text-[var(--text-primary)] focus:outline-none focus:ring-2 focus:ring-[var(--accent-color)]"
                          placeholder="Plum Audio"
                          disabled={dlnaEndpointNameStatuses[endpoint.id] === 'applying'}
                        />
                        {nameChanged && (
                          <button
                            onClick={() => handleDlnaEndpointNameChange(endpoint.id)}
                            disabled={nameEmpty || dlnaEndpointNameStatuses[endpoint.id] === 'applying'}
                            className="absolute right-1.5 top-1/2 -translate-y-1/2 px-2 py-0.5 text-xs bg-[var(--accent-color)] accent-button-text rounded hover:opacity-80 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {dlnaEndpointNameStatuses[endpoint.id] === 'applying' ? 'Applying...' : 'Apply'}
                          </button>
                        )}
                      </div>
                      {dlnaEndpointNameMessages[endpoint.id] && (
                        <p className={`text-xs mt-1 ${
                          dlnaEndpointNameStatuses[endpoint.id] === 'success'
                            ? 'text-green-500'
                            : dlnaEndpointNameStatuses[endpoint.id] === 'error'
                            ? 'text-red-500'
                            : 'text-[var(--text-muted)]'
                        }`}>
                          {dlnaEndpointNameMessages[endpoint.id]}
                        </p>
                      )}
                      {nameChanged && !dlnaEndpointNameMessages[endpoint.id] && (
                        <p className="text-xs text-amber-500 mt-1">
                          Pending changes
                        </p>
                      )}
                    </div>
                    <div className="text-xs text-[var(--text-muted)] mt-2">
                      UPnP Port: {endpoint.port}
                    </div>
                  </div>
                );
              })}

              {/* Add endpoint button/form */}
              {!showAddDlnaEndpoint && dlnaEndpoints.length < 10 && (
                <button
                  onClick={() => setShowAddDlnaEndpoint(true)}
                  disabled={isAnyDlnaEndpointBeingEdited}
                  className="w-full px-3 py-2 bg-[var(--bg-secondary)] hover:bg-[var(--bg-tertiary-hover)] border border-[var(--border-color)] rounded text-sm text-[var(--text-secondary)] transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  + Add Endpoint
                </button>
              )}

              {showAddDlnaEndpoint && (
                <div className="p-3 bg-[var(--bg-secondary)] rounded border border-[var(--accent-color)]">
                  <label className="block text-xs text-[var(--text-secondary)] mb-1">
                    New Endpoint Name
                  </label>
                  <div className="flex gap-2">
                    <input
                      type="text"
                      value={newDlnaEndpointName}
                      onChange={(e) => setNewDlnaEndpointName(e.target.value)}
                      onKeyDown={(e) => e.key === 'Enter' && newDlnaEndpointName.trim() && !isAnyDlnaEndpointBeingEdited && handleAddDlnaEndpoint()}
                      className="flex-1 px-2 py-1.5 bg-[var(--bg-primary)] border border-[var(--border-color)] rounded text-sm text-[var(--text-primary)] focus:outline-none focus:ring-2 focus:ring-[var(--accent-color)]"
                      placeholder="Living Room"
                      disabled={isAnyDlnaEndpointBeingEdited || isAddingDlnaEndpoint}
                    />
                    <button
                      onClick={handleAddDlnaEndpoint}
                      disabled={!newDlnaEndpointName.trim() || isAnyDlnaEndpointBeingEdited || isAddingDlnaEndpoint}
                      className="px-3 py-1.5 bg-[var(--accent-color)] accent-button-text rounded text-xs hover:opacity-80 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      {isAddingDlnaEndpoint ? 'Adding...' : 'Add'}
                    </button>
                    <button
                      onClick={() => {setShowAddDlnaEndpoint(false); setNewDlnaEndpointName('');}}
                      disabled={isAddingDlnaEndpoint}
                      className="px-3 py-1.5 bg-[var(--bg-tertiary)] hover:bg-[var(--bg-tertiary-hover)] rounded text-xs text-[var(--text-secondary)] disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}

              {dlnaEndpoints.length >= 10 && (
                <p className="text-xs text-[var(--text-muted)] italic">
                  Maximum of 10 endpoints reached
                </p>
              )}
                  </>
                );
              })()}
            </div>
          )}
        </div>

        {/* Plexamp */}
        <div className={`p-4 bg-[var(--bg-tertiary)] rounded-lg border border-[var(--border-color)] ${!settings.integrations.plexamp.available ? 'opacity-50' : ''}`}>
          <div className="flex items-start justify-between gap-4">
            {/* Left: Icon */}
            <div className="w-12 flex justify-center items-center flex-shrink-0">
              <Icon name="plexamp" className="text-[2.5rem] text-[var(--text-secondary)]" style={{ color: 'inherit' }} aria-hidden />
            </div>

            {/* Middle: Title + Description */}
            <div className="flex flex-col flex-1">
              <span className="text-base font-semibold text-[var(--text-secondary)]">Plexamp</span>
              <p className="text-sm text-[var(--text-muted)] mt-1">
                Plex music player integration
              </p>
              {!settings.integrations.plexamp.available && (
                <p className="text-xs text-amber-500 mt-1">
                  Not configured - set PLEXAMP_ENABLED in .env file
                </p>
              )}
              {isTogglingPlexamp && (
                <p className="text-xs text-amber-500 mt-1">
                  Processing... this may take up to 60 seconds
                </p>
              )}
            </div>

            {/* Right: Toggle */}
            <div className="flex items-center flex-shrink-0">
              <div className="relative">
                <input
                  type="checkbox"
                  className="sr-only"
                  checked={settings.integrations.plexamp.enabled}
                  onChange={(e) => handlePlexampToggle(e.target.checked)}
                  disabled={!settings.integrations.plexamp.available || isTogglingPlexamp}
                  id="plexamp-toggle"
                />
                <label
                  htmlFor="plexamp-toggle"
                  className={`block w-12 h-6 rounded-full transition ${!settings.integrations.plexamp.available || isTogglingPlexamp ? 'cursor-not-allowed opacity-50' : 'cursor-pointer'} ${settings.integrations.plexamp.enabled ? 'bg-[var(--accent-color)]' : 'bg-[var(--bg-tertiary-hover)]'}`}
                >
                  <div className={`dot absolute left-1 top-1 bg-white w-4 h-4 rounded-full transition-transform ${settings.integrations.plexamp.enabled ? 'translate-x-6' : ''}`}></div>
                </label>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className="pt-4 mt-6 border-t border-[var(--border-color)]">
        <p className="text-xs text-[var(--text-muted)] italic">
          Note: Changes to integration settings will be applied after service restart.
        </p>
      </div>
    </div>
  );
};
