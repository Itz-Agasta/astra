export type Vec3 = [number, number, number];

export type StellariumLocation = {
  name: string;
  planet: string;
  latitude: number;
  longitude: number;
  altitude: number;
};

export type StellariumTime = {
  jday: number;
  utc: string;
  local: string;
  timeZone: string;
};

export type StellariumStatus = {
  location: StellariumLocation;
  time: StellariumTime;
  selectioninfo: string;
  view: { fov: number };
};

export type StellariumView = {
  j2000?: string;
  altAz?: string;
};

export type ObjectInfo = {
  found?: boolean;
  name?: string;
  "localized-name"?: string;
  raJ2000?: number;
  decJ2000?: number;
};

export type SweEngine = {
  core: {
    observer: {
      longitude: number;
      latitude: number;
      elevation: number;
      utc: number;
      yaw: number;
      pitch: number;
    };
    fov: number;
    stars: { addDataSource: (opts: { url: string; key?: string }) => void };
    skycultures: { addDataSource: (opts: { url: string; key?: string }) => void };
    milkyway: { addDataSource: (opts: { url: string; key?: string }) => void };
    planets: { addDataSource: (opts: { url: string; key?: string }) => void };
    dsos?: { addDataSource: (opts: { url: string; key?: string }) => void };
    landscapes?: { visible: boolean };
    atmosphere?: { visible: boolean };
    selection?: unknown;
  };
  lookAt: (pos: Vec3, duration?: number) => void;
  zoomTo: (fov: number, duration?: number) => void;
  convertFrame: (obs: SweEngine["core"]["observer"], origin: string, dest: string, v: Vec3) => number[];
  setFont?: (name: string, url: string) => Promise<unknown>;
  getObj?: (name: string) => unknown;
  pointAndLock?: (target: unknown, duration?: number) => void;
};

declare global {
  interface Window {
    StelWebEngine: (opts: {
      wasmFile: string;
      canvas: HTMLCanvasElement;
      canvasElement?: HTMLCanvasElement;
      onReady?: (stel: SweEngine) => void;
    }) => Promise<SweEngine> | SweEngine;
  }
}

export {};
