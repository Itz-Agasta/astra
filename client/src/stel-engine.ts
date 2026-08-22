import type { SweEngine, Vec3 } from "./types";

let stel: SweEngine | null = null;

export function getEngine(): SweEngine | null {
  return stel;
}

function baseUrl(): string {
  return `${window.location.origin}/stel/skydata/`;
}

export async function initEngine(canvas: HTMLCanvasElement): Promise<SweEngine> {
  if (stel) {
    return stel;
  }
  if (typeof window.StelWebEngine !== "function") {
    throw new Error("StelWebEngine glue JS did not load");
  }

  const engine = await new Promise<SweEngine>((resolve, reject) => {
    const timeout = window.setTimeout(() => reject(new Error("engine init timeout")), 30000);
    try {
      window.StelWebEngine({
        wasmFile: "/stel/stellarium-web-engine.wasm",
        canvas,
        canvasElement: canvas,
        onReady: (instance) => {
          window.clearTimeout(timeout);
          resolve(instance);
        },
      });
    } catch (err) {
      window.clearTimeout(timeout);
      reject(err);
    }
  });

  const data = baseUrl();
  const core = engine.core;
  core.stars.addDataSource({ url: `${data}stars/base` });
  core.stars.addDataSource({ url: `${data}stars/minimal` });
  core.skycultures.addDataSource({ url: `${data}skycultures/western`, key: "western" });
  core.milkyway.addDataSource({ url: `${data}surveys/milkyway` });
  core.planets.addDataSource({ url: `${data}surveys/sso/sun`, key: "sun" });
  core.planets.addDataSource({ url: `${data}surveys/sso/moon`, key: "moon" });
  core.planets.addDataSource({ url: `${data}surveys/sso/moon`, key: "default" });
  if (core.landscapes) {
    core.landscapes.visible = false;
  }
  if (core.atmosphere) {
    core.atmosphere.visible = true;
  }

  stel = engine;
  return engine;
}

let lastLook: string | null = null;

export function applyPointing(j2000: Vec3, fovDeg: number): void {
  if (!stel) {
    return;
  }
  // lookAt takes FRAME_OBSERVED (alt/az), not ICRF/J2000.
  const converted = stel.convertFrame(stel.core.observer, "ICRF", "OBSERVED", j2000);
  const ox = converted[0];
  const oy = converted[1];
  const oz = converted[2];
  if (ox === undefined || oy === undefined || oz === undefined || ![ox, oy, oz].every(Number.isFinite)) {
    return;
  }
  const observed: Vec3 = [ox, oy, oz];
  const key = `${ox.toFixed(5)},${oy.toFixed(5)},${oz.toFixed(5)},${fovDeg.toFixed(3)}`;
  if (key === lastLook) {
    return;
  }
  lastLook = key;
  stel.lookAt(observed, 0);
  const fovRad = (fovDeg * Math.PI) / 180;
  if (typeof stel.zoomTo === "function") {
    stel.zoomTo(fovRad, 0);
  } else {
    stel.core.fov = fovRad;
  }
}

export function applyObserver(latDeg: number, lonDeg: number, elevM: number, jd: number): void {
  if (!stel) {
    return;
  }
  const obs = stel.core.observer;
  obs.latitude = (latDeg * Math.PI) / 180;
  obs.longitude = (lonDeg * Math.PI) / 180;
  obs.elevation = elevM;
  obs.utc = jd - 2400000.5;
}

let desktopIcrs: Vec3 | null = null;

export function setDesktopSelection(icrs: Vec3 | null): void {
  desktopIcrs = icrs;
}

export function raDecToIcrs(raDeg: number, decDeg: number): Vec3 {
  const ra = (raDeg * Math.PI) / 180;
  const dec = (decDeg * Math.PI) / 180;
  const c = Math.cos(dec);
  return [c * Math.cos(ra), c * Math.sin(ra), Math.sin(dec)];
}

/** Stereographic project ICRF → canvas CSS pixels (SWE default projection). */
export function projectDesktopSelection(
  width: number,
  height: number,
): { x: number; y: number } | null {
  if (!stel || !desktopIcrs || width <= 0 || height <= 0) {
    return null;
  }
  const view = stel.convertFrame(stel.core.observer, "ICRF", "VIEW", desktopIcrs);
  const vx = view[0];
  const vy = view[1];
  const vz = view[2];
  if (vx === undefined || vy === undefined || vz === undefined || ![vx, vy, vz].every(Number.isFinite)) {
    return null;
  }
  const n = Math.hypot(vx, vy, vz);
  if (n < 1e-12) {
    return null;
  }
  const x = vx / n;
  const y = vy / n;
  const z = vz / n;
  // VIEW looks down −Z; z → +1 is the stereographic discontinuity (behind).
  if (z >= 0.999) {
    return null;
  }
  const denom = 1 - z;
  if (Math.abs(denom) < 1e-12) {
    return null;
  }
  const aspect = width / height;
  const fov = stel.core.fov;
  const fovy = aspect < 1 ? 4 * Math.atan(Math.tan(fov / 4) / aspect) : fov;
  const k = 1 / Math.tan(fovy / 4);
  const xNdc = (k / aspect) * (x / (1 - z));
  const yNdc = k * (y / (1 - z));
  if (![xNdc, yNdc].every(Number.isFinite)) {
    return null;
  }
  const sx = ((xNdc + 1) / 2) * width;
  const sy = ((-yNdc + 1) / 2) * height;
  if (sx < 0 || sy < 0 || sx > width || sy > height) {
    return null;
  }
  return { x: sx, y: sy };
}
