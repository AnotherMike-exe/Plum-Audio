import React from 'react';
import { Icon, type IconName } from './Icon';

interface SwitchProps {
    checked: boolean;
    onChange: (checked: boolean) => void;
    label: string;
    icon?: string;
    disabled?: boolean;
}

export const Switch: React.FC<SwitchProps> = ({checked, onChange, label, icon, disabled = false}) => {
    const switchId = `switch-${label.replace(/\s+/g, '-').toLowerCase()}`;

    return (
        <label htmlFor={switchId}
               className={`flex items-center justify-between p-2 rounded-lg ${disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer hover:bg-[var(--bg-tertiary)]'}`}>
            <div className="flex items-center gap-6">
                {icon && (
                    <div className="w-8 flex justify-center">
                        <Icon name={icon.replace('fa-', '') as IconName} className="text-lg text-[var(--text-secondary)]" style={{ color: 'inherit' }} aria-hidden />
                    </div>
                )}
                <span className="text-base text-[var(--text-secondary)]">{label}</span>
            </div>
            <div className="relative ml-4">
                <input
                    id={switchId}
                    type="checkbox"
                    className="sr-only"
                    checked={checked}
                    onChange={(e) => onChange(e.target.checked)}
                    disabled={disabled}
                />
                <div
                    className={`block w-12 h-6 rounded-full transition ${checked ? 'bg-[var(--accent-color)]' : 'bg-[var(--bg-tertiary-hover)]'}`}></div>
                <div
                    className={`dot absolute left-1 top-1 bg-white w-4 h-4 rounded-full transition-transform ${checked ? 'translate-x-6' : ''}`}
                ></div>
            </div>
        </label>
    );
};