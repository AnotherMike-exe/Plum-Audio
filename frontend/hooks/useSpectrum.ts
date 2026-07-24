import { useEffect, useRef, useState } from 'react';
import type { VizFrame } from '../services/sendspinControllerClient';
import type { AudioVisualizerData } from './useAudioVisualizer';

/**
 * Feed the visualizer from the native Sendspin visualizer role instead of browser WebAudio.
 *
 * The server computes the spectrum (see docs/ARCHITECTURE.md) and the controller client exposes the
 * latest frame; `getFrame` reads it for the currently featured source. We poll on
 * requestAnimationFrame — exactly as the old browser-FFT path did — so the ~30 fps frame stream
 * drives the canvas directly without pushing every frame through React state. Returns the same
 * `{ frequencyData }` shape the ported canvas components already consume, or null when no source is
 * streaming (the canvas renders its idle state).
 */
export function useSpectrum(getFrame: (() => VizFrame | null) | null, enabled: boolean): AudioVisualizerData | null {
  const [data, setData] = useState<AudioVisualizerData | null>(null);
  const raf = useRef<number | undefined>(undefined);

  useEffect(() => {
    if (!enabled || !getFrame) {
      setData(null);
      return;
    }
    const tick = () => {
      const frame = getFrame();
      // A frame older than ~250 ms means the source stopped feeding us between animation frames;
      // treat it as idle so the bars fall to rest rather than freezing on the last spectrum.
      const fresh = frame && Date.now() - frame.at < 250;
      setData(fresh ? { frequencyData: frame!.spectrum, timestamp: frame!.at } : null);
      raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
    return () => {
      if (raf.current) cancelAnimationFrame(raf.current);
    };
  }, [enabled, getFrame]);

  return data;
}
