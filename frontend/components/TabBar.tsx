import React from 'react';
import { Icon, type IconName } from './Icon';

export type Tab = {
  id: string;
  label: string;
  icon: IconName;
};

interface TabBarProps {
  tabs: Tab[];
  activeTab: string;
  onTabChange: (tabId: string) => void;
}

export const TabBar: React.FC<TabBarProps> = ({ tabs, activeTab, onTabChange }) => {
  return (
    <div className="flex flex-wrap border-b border-[var(--border-color)] bg-[var(--bg-secondary)]">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          onClick={() => onTabChange(tab.id)}
          className={`
            flex items-center gap-2 px-4 py-3 font-semibold text-sm transition-colors
            border-b-2 -mb-px whitespace-nowrap
            ${
              activeTab === tab.id
                ? 'border-[var(--accent-color)] text-[var(--accent-color)]'
                : 'border-transparent text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:border-[var(--border-color)]'
            }
          `}
          aria-selected={activeTab === tab.id}
          role="tab"
        >
          <Icon name={tab.icon} aria-hidden className="text-lg" style={{ color: 'inherit' }} />
          <span>{tab.label}</span>
        </button>
      ))}
    </div>
  );
};
