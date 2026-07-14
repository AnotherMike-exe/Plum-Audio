import React, {useState, useEffect, useRef} from 'react';
import type {Server, Settings as SettingsType} from '../../types';
import {Switch} from '../Switch';
import {ServerManager} from '../ServerManager';
import {federationService} from '../../services/federationService';

interface SnapcastTabProps {
  settings: SettingsType;
  onSettingsChange: (newSettings: SettingsType) => void;
}

export const SnapcastTab: React.FC<SnapcastTabProps> = ({
  settings,
  onSettingsChange,
}) => {
  const [servers, setServers] = useState<Server[]>([]);
  const [isTogglingFederation, setIsTogglingFederation] = useState(false);
  const [isTogglingAutoDiscover, setIsTogglingAutoDiscover] = useState(false);
  const [isServerOperationInProgress, setIsServerOperationInProgress] = useState(false);
  const isTogglingFederationRef = useRef(false);

  const fetchServers = async () => {
    if (settings.federation.enabled) {
      const serverList = await federationService.getServers();
      setServers(serverList);
    } else {
      setServers([]);
    }
  };

  useEffect(() => {
    // Only auto-fetch if not currently toggling (toggle handler manages the timing)
    // Use ref to avoid race condition with state updates
    if (!isTogglingFederationRef.current) {
      fetchServers();
    }
  }, [settings.federation.enabled]);

  const handleFederationToggle = async (enabled: boolean) => {
    setIsTogglingFederation(true);
    isTogglingFederationRef.current = true;
    try {
      onSettingsChange({
        ...settings,
        federation: {
          ...settings.federation,
          enabled,
        },
      });

      // If enabling federation, wait for backend to restart and be ready
      if (enabled) {
        console.log('[Federation] Waiting for backend to be ready...');

        // Poll the backend until it's ready (with timeout)
        const maxAttempts = 20; // 20 attempts * 500ms = 10 seconds max
        let attempt = 0;
        let isReady = false;

        while (attempt < maxAttempts && !isReady) {
          attempt++;
          await new Promise(resolve => setTimeout(resolve, 500));

          // Check if federation service is healthy
          const healthy = await federationService.checkHealth();
          if (healthy) {
            // Backend is healthy, now check if servers are connected
            const serverList = await federationService.getServers();
            const hasConnectedServers = serverList.some(s => s.connected);

            if (hasConnectedServers) {
              isReady = true;
              console.log(`[Federation] Backend ready with ${serverList.length} server(s) after ${attempt * 0.5}s`);
              // Update UI with the servers
              setServers(serverList);
            } else if (attempt === maxAttempts) {
              console.error('Federation backend did not connect to local server in time');
              throw new Error('Federation service started but could not connect to local server. Please try again.');
            }
          } else if (attempt === maxAttempts) {
            console.error('Federation backend did not become ready in time');
            throw new Error('Federation service did not start. Please try again.');
          }
        }
      }
    } catch (error) {
      console.error('Failed to toggle federation:', error);
      alert(error instanceof Error ? error.message : 'Error toggling Multi-Server Control');
    } finally {
      setIsTogglingFederation(false);
      isTogglingFederationRef.current = false;
    }
  };

  const handleAutoDiscoverToggle = async (enabled: boolean) => {
    setIsTogglingAutoDiscover(true);
    try {
      onSettingsChange({
        ...settings,
        federation: {
          ...settings.federation,
          autoDiscover: enabled,
        },
      });
    } catch (error) {
      console.error('Failed to toggle auto-discover:', error);
      alert('Error toggling Auto-Discover');
    } finally {
      setIsTogglingAutoDiscover(false);
    }
  };

  const handleDisplayChange = (key: keyof SettingsType['display'], value: boolean) => {
    onSettingsChange({
      ...settings,
      display: {
        ...settings.display,
        [key]: value,
      },
    });
  };

  const handleAddServer = async (host: string, port: number, name: string) => {
    setIsServerOperationInProgress(true);
    try {
      const result = await federationService.addServer(host, port, name);
      if (result.success) {
        // Refresh server list to get updated connection status
        // Use a slight delay to ensure backend has time to establish connection
        setTimeout(() => {
          fetchServers();
          setIsServerOperationInProgress(false);
        }, 1000);
      } else {
        setIsServerOperationInProgress(false);
      }
      return result;
    } catch (error) {
      setIsServerOperationInProgress(false);
      throw error;
    }
  };

  const handleEditServer = async (serverId: string, host: string, port: number, name: string) => {
    setIsServerOperationInProgress(true);
    try {
      const result = await federationService.editServer(serverId, host, port, name);
      if (result.success) {
        // Refresh server list to get updated connection status
        // Use a slight delay to ensure backend has time to reconnect
        setTimeout(() => {
          fetchServers();
          setIsServerOperationInProgress(false);
        }, 1000);
      } else {
        setIsServerOperationInProgress(false);
      }
      return result;
    } catch (error) {
      setIsServerOperationInProgress(false);
      throw error;
    }
  };

  const handleRemoveServer = async (serverId: string) => {
    setIsServerOperationInProgress(true);
    try {
      const result = await federationService.removeServer(serverId);
      if (result.success) {
        // Refresh server list to ensure clean state
        await fetchServers();
      }
      return result;
    } finally {
      setIsServerOperationInProgress(false);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-base font-semibold text-[var(--text-primary)] mb-4">
          Multi-Server Federation
        </h3>
        <p className="text-sm text-[var(--text-muted)] mb-6">
          Control multiple Snapcast servers from a single interface
        </p>
      </div>

      <div className="space-y-4">
        <Switch
          label="Multi-Server Control"
          checked={settings.federation.enabled}
          onChange={handleFederationToggle}
          icon="network-wired"
          disabled={isTogglingFederation}
        />

        {isTogglingFederation && (
          <p className="text-xs text-amber-500 ml-8 mt-1">
            Processing...
          </p>
        )}

        {settings.federation.enabled && (
          <>
            <div className="pl-8 space-y-4">
              <Switch
                label="Auto-Discover Servers"
                checked={settings.federation.autoDiscover}
                onChange={handleAutoDiscoverToggle}
                disabled={isTogglingAutoDiscover}
              />

              {isTogglingAutoDiscover && (
                <p className="text-xs text-amber-500 ml-8 mt-1">
                  Processing...
                </p>
              )}

              <div className="pt-2">
                <p className="text-sm text-[var(--text-muted)] mb-4">
                  Server name is set in Settings → About → Device Settings. Other servers will see this device as "{settings.deviceName}".
                </p>
              </div>

              <div>
                <ServerManager
                  servers={servers}
                  onAddServer={handleAddServer}
                  onEditServer={handleEditServer}
                  onRemoveServer={handleRemoveServer}
                />
                {isServerOperationInProgress && (
                  <p className="text-xs text-amber-500 mt-2">
                    Processing server operation...
                  </p>
                )}
              </div>
            </div>
          </>
        )}
      </div>

      <div className="pt-4 mt-6 border-t border-[var(--border-color)]">
        <h4 className="text-sm font-semibold text-[var(--text-primary)] mb-4">Display Options</h4>
        <div className="space-y-4">
          <Switch
            label="Show Offline Devices"
            checked={settings.display.showOfflineDevices}
            onChange={(val) => handleDisplayChange('showOfflineDevices', val)}
            icon="eye"
          />
          <p className="text-xs text-[var(--text-muted)] pl-8 -mt-2">
            Display devices that are currently disconnected or unreachable
          </p>
        </div>
      </div>

      <div className="pt-4 mt-6 border-t border-[var(--border-color)]">
        <h4 className="text-sm font-semibold text-[var(--text-primary)] mb-2">
          About Federation
        </h4>
        <p className="text-xs text-[var(--text-muted)]">
          Federation allows you to control multiple Snapcast servers from one interface. When enabled,
          you can see and control clients across all connected servers, making it easy to manage
          multi-room audio across different locations.
        </p>
      </div>
    </div>
  );
};
