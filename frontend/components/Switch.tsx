import React from 'react';
import { Icon, type IconName } from './Icon';

interface SwitchProps {
    checked: boolean;
    onChange: (checked: boolean) => void;
    label: string;
    icon?: string;
    disabled?: boolean;
    description?: string;
}

export const Switch: React.FC<SwitchProps> = ({checked, onChange, label, icon, disabled = false, description}) => {
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
                <div className="flex flex-col">
                    <span className="text-base text-[var(--text-secondary)]">{label}</span>
                    {description && (
                        <span className="text-xs text-[var(--text-muted)] mt-0.5">{description}</span>
                    )}
                </div>
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