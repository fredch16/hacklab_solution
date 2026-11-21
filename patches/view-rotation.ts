import { useSyncExternalStore } from 'react';

// Rotation of the default 3D arm view, in degrees counter-clockwise as seen
// from above (about the scene's +Y up axis). Cycles 0 -> 90 -> 180 -> 270.
export type ViewRotation = 0 | 90 | 180 | 270;

const STORAGE_KEY = 'station-viewer.st3215.viewRotation';
const DEFAULT_ROTATION: ViewRotation = 90;

function load(): ViewRotation {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    const value = stored === null ? NaN : Number(stored);
    return value === 0 || value === 90 || value === 180 || value === 270 ? value : DEFAULT_ROTATION;
  } catch {
    return DEFAULT_ROTATION;
  }
}

let current: ViewRotation = load();
const listeners = new Set<() => void>();

export function cycleViewRotation() {
  current = ((current + 90) % 360) as ViewRotation;
  try {
    window.localStorage.setItem(STORAGE_KEY, String(current));
  } catch {
    // storage unavailable; keep in-memory state only
  }
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useViewRotation(): ViewRotation {
  return useSyncExternalStore(subscribe, () => current);
}
