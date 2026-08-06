import React, {useState} from 'react';
import type {Settings as SettingsType} from '../types';
import {TabBar, type Tab} from './TabBar';
import {IntegrationsTab} from './settings/IntegrationsTab';
import {AudioTab} from './settings/AudioTab';
import {PlaybackTab} from './settings/PlaybackTab';
import {ThemeTab} from './settings/ThemeTab';
import {VisualizerTab} from './settings/VisualizerTab';
import {AboutTab} from './settings/AboutTab';
import { Icon } from './Icon';

interface SettingsProps {
    settings: SettingsType;
    onSettingsChange: (newSettings: SettingsType) => void;
    onClose: () => void;
    initialTab?: string; // Optional initial tab to open
}

// Phase 3: Integrations is surfaced with only the sources whose backend exists (AirPlay, Spotify
// and Bluetooth today — see the enabledSources prop below). A source whose backend lands but which
// is NOT added to that array is invisible in Settings with no other symptom, so add it here as part
// of the slice, not afterwards. Playback (auto-route-on-connect + auto-follow, mesh/follow.py)
// landed — re-added here.
//
// Tab ORDER is the layout, and the overlay opens on the leftmost tab: Integrations first (what this
// unit can receive), then Audio (what it plays through), then Playback (the rules about which
// source it picks up), then presentation.
const tabs: Tab[] = [
    {id: 'integrations', label: 'Integrations', icon: 'puzzle-piece'},
    {id: 'audio', label: 'Audio', icon: 'volume-high'},
    {id: 'playback', label: 'Playback', icon: 'network-wired'},
    {id: 'theme', label: 'Theme', icon: 'palette'},
    {id: 'visualizer', label: 'Visualizer', icon: 'waveform'},
    {id: 'about', label: 'About', icon: 'circle-info'},
];

export const Settings: React.FC<SettingsProps> = ({settings, onSettingsChange, onClose, initialTab}) => {
    const [activeTab, setActiveTab] = useState(initialTab || tabs[0].id);

    const renderTabContent = () => {
        switch (activeTab) {
            case 'integrations':
                return <IntegrationsTab settings={settings} onSettingsChange={onSettingsChange} enabledSources={['airplay', 'spotify', 'bluetooth']} />;
            case 'audio':
                return <AudioTab settings={settings} onSettingsChange={onSettingsChange} />;
            case 'playback':
                return <PlaybackTab settings={settings} onSettingsChange={onSettingsChange} />;
            case 'theme':
                return <ThemeTab settings={settings} onSettingsChange={onSettingsChange} />;
            case 'visualizer':
                return <VisualizerTab settings={settings} onSettingsChange={onSettingsChange} />;
            case 'about':
                return <AboutTab settings={settings} onSettingsChange={onSettingsChange} />;
            default:
                return null;
        }
    };

    return (
        <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
            onClick={onClose}
            role="dialog"
            aria-modal="true"
            aria-labelledby="settings-title"
        >
            <div
                className="relative w-[80vw] h-[80vh] m-4 bg-[var(--bg-secondary)] rounded-2xl shadow-2xl border border-[var(--border-color)] flex flex-col"
                onClick={e => e.stopPropagation()}
            >
                <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border-color)]">
                    <h2 id="settings-title" className="text-2xl font-bold text-[var(--text-primary)]">Settings</h2>
                    <button
                        onClick={onClose}
                        className="w-8 h-8 flex items-center justify-center rounded-full text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]"
                        aria-label="Close settings"
                    >
                        <Icon name="xmark" />
                    </button>
                </div>

                <TabBar tabs={tabs} activeTab={activeTab} onTabChange={setActiveTab} />

                <div className="p-6 overflow-y-auto flex-1">
                    {renderTabContent()}
                </div>
            </div>
        </div>
    );
};