import type { CSSProperties } from 'react';

// Import all SVG icons as raw strings
import playIcon from '../src/assets/icons/play.svg?raw';
import pauseIcon from '../src/assets/icons/pause.svg?raw';
import forwardStepIcon from '../src/assets/icons/forward-step.svg?raw';
import backwardStepIcon from '../src/assets/icons/backward-step.svg?raw';
import volumeHighIcon from '../src/assets/icons/volume-high.svg?raw';
import volumeLowIcon from '../src/assets/icons/volume-low.svg?raw';
import volumeXmarkIcon from '../src/assets/icons/volume-xmark.svg?raw';
import chevronDownIcon from '../src/assets/icons/chevron-down.svg?raw';
import chevronUpIcon from '../src/assets/icons/chevron-up.svg?raw';
import xmarkIcon from '../src/assets/icons/xmark.svg?raw';
import gearIcon from '../src/assets/icons/gear.svg?raw';
import spinnerIcon from '../src/assets/icons/spinner.svg?raw';
import musicIcon from '../src/assets/icons/music.svg?raw';
import triangleExclamationIcon from '../src/assets/icons/triangle-exclamation.svg?raw';
import desktopIcon from '../src/assets/icons/desktop.svg?raw';
import towerBroadcastIcon from '../src/assets/icons/tower-broadcast.svg?raw';
import networkWiredIcon from '../src/assets/icons/network-wired.svg?raw';
import sunIcon from '../src/assets/icons/sun.svg?raw';
import moonIcon from '../src/assets/icons/moon.svg?raw';
import paletteIcon from '../src/assets/icons/palette.svg?raw';
import headphonesIcon from '../src/assets/icons/headphones.svg?raw';
import plusIcon from '../src/assets/icons/plus.svg?raw';
import trashIcon from '../src/assets/icons/trash.svg?raw';
import circleInfoIcon from '../src/assets/icons/circle-info.svg?raw';
import puzzlePieceIcon from '../src/assets/icons/puzzle-piece.svg?raw';
import bookIcon from '../src/assets/icons/book.svg?raw';
import githubIcon from '../src/assets/icons/github.svg?raw';
import spotifyIcon from '../src/assets/icons/spotify.svg?raw';
import appleIcon from '../src/assets/icons/apple.svg?raw';
import bluetoothIcon from '../src/assets/icons/bluetooth.svg?raw';
import waveformIcon from '../src/assets/icons/waveform.svg?raw';
import plexampIcon from '../src/assets/icons/plexamp.svg?raw';
import stopIcon from '../src/assets/icons/stop.svg?raw';
import circleExclamationIcon from '../src/assets/icons/circle-exclamation.svg?raw';
import shuffleIcon from '../src/assets/icons/shuffle.svg?raw';
import repeatIcon from '../src/assets/icons/repeat.svg?raw';

export type IconName =
  | 'play'
  | 'pause'
  | 'forward-step'
  | 'backward-step'
  | 'volume-high'
  | 'volume-low'
  | 'volume-xmark'
  | 'chevron-down'
  | 'chevron-up'
  | 'xmark'
  | 'gear'
  | 'spinner'
  | 'music'
  | 'triangle-exclamation'
  | 'desktop'
  | 'tower-broadcast'
  | 'network-wired'
  | 'sun'
  | 'moon'
  | 'palette'
  | 'headphones'
  | 'plus'
  | 'trash'
  | 'circle-info'
  | 'puzzle-piece'
  | 'book'
  | 'github'
  | 'spotify'
  | 'apple'
  | 'bluetooth'
  | 'waveform'
  | 'plexamp'
  | 'stop'
  | 'circle-exclamation'
  | 'shuffle'
  | 'repeat';

const iconMap: Record<IconName, string> = {
  'play': playIcon,
  'pause': pauseIcon,
  'forward-step': forwardStepIcon,
  'backward-step': backwardStepIcon,
  'volume-high': volumeHighIcon,
  'volume-low': volumeLowIcon,
  'volume-xmark': volumeXmarkIcon,
  'chevron-down': chevronDownIcon,
  'chevron-up': chevronUpIcon,
  'xmark': xmarkIcon,
  'gear': gearIcon,
  'spinner': spinnerIcon,
  'music': musicIcon,
  'triangle-exclamation': triangleExclamationIcon,
  'desktop': desktopIcon,
  'tower-broadcast': towerBroadcastIcon,
  'network-wired': networkWiredIcon,
  'sun': sunIcon,
  'moon': moonIcon,
  'palette': paletteIcon,
  'headphones': headphonesIcon,
  'plus': plusIcon,
  'trash': trashIcon,
  'circle-info': circleInfoIcon,
  'puzzle-piece': puzzlePieceIcon,
  'book': bookIcon,
  'github': githubIcon,
  'spotify': spotifyIcon,
  'apple': appleIcon,
  'bluetooth': bluetoothIcon,
  'waveform': waveformIcon,
  'plexamp': plexampIcon,
  'stop': stopIcon,
  'circle-exclamation': circleExclamationIcon,
  'shuffle': shuffleIcon,
  'repeat': repeatIcon,
};

interface IconProps {
  name: IconName;
  className?: string;
  style?: CSSProperties;
  spin?: boolean;
  'aria-hidden'?: boolean;
  'aria-label'?: string;
}

export function Icon({
  name,
  className = '',
  style,
  spin = false,
  'aria-hidden': ariaHidden,
  'aria-label': ariaLabel
}: IconProps) {
  const iconSvg = iconMap[name];
  const combinedClassName = `icon${spin ? ' icon-spin' : ''}${className ? ` ${className}` : ''}`;

  return (
    <span
      className={combinedClassName}
      style={{
        display: 'inline-block',
        width: '1em',
        height: '1em',
        verticalAlign: '-0.125em',
        fill: 'currentColor',
        ...style
      }}
      aria-hidden={ariaHidden}
      aria-label={ariaLabel}
      dangerouslySetInnerHTML={{ __html: iconSvg }}
    />
  );
}
