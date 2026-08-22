import { applyObserver, applyPointing, setDesktopSelection } from "./stel-engine";
import type { ObjectInfo, StellariumStatus, StellariumView, Vec3 } from "./types";

export type DesktopState = {
  status: StellariumStatus;
  view: StellariumView;
  j2000: Vec3;
  meteors: string[];
  online: true;
};

const POLL_MS = 250;

function parseVec3(raw: string | undefined): Vec3 | null {
  if (!raw) {
    return null;
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed) || parsed.length !== 3) {
      return null;
    }
    const x = Number(parsed[0]);
    const y = Number(parsed[1]);
    const z = Number(parsed[2]);
    if (![x, y, z].every(Number.isFinite)) {
      return null;
    }
    return [x, y, z];
  } catch {
    return null;
  }
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) {
    throw new Error(`${path} ${res.status}`);
  }
  return (await res.json()) as T;
}

let activeMeteors: string[] = [];
let lastMeteorsFetch = 0;

async function fetchMeteorsIfNeeded(): Promise<string[]> {
  const now = Date.now();
  if (now - lastMeteorsFetch < 10000 && activeMeteors.length > 0) {
    return activeMeteors;
  }
  try {
    const list = await getJson<string[]>("/api/objects/listobjectsbytype?type=MeteorShowers");
    if (Array.isArray(list)) {
      activeMeteors = list;
      lastMeteorsFetch = now;
    }
  } catch (e) {
    console.warn("Failed to fetch active meteor showers list", e);
  }
  return activeMeteors;
}

async function selectionIcrs(): Promise<Vec3 | null> {
  try {
    const info = await getJson<ObjectInfo>("/api/objects/info?format=json");
    if (info.found === false) {
      return null;
    }
    const ra = info.raJ2000;
    const dec = info.decJ2000;
    if (ra === undefined || dec === undefined || ![ra, dec].every(Number.isFinite)) {
      return null;
    }
    const raRad = (ra * Math.PI) / 180;
    const decRad = (dec * Math.PI) / 180;
    const c = Math.cos(decRad);
    return [c * Math.cos(raRad), c * Math.sin(raRad), Math.sin(decRad)];
  } catch {
    return null;
  }
}

export async function pollDesktop(): Promise<DesktopState> {
  const [status, view, meteors] = await Promise.all([
    getJson<StellariumStatus>("/api/main/status"),
    getJson<StellariumView>("/api/main/view"),
    fetchMeteorsIfNeeded()
  ]);
  const j2000 = parseVec3(view.j2000);
  if (!j2000) {
    throw new Error("invalid j2000 view");
  }
  applyObserver(
    status.location.latitude,
    status.location.longitude,
    status.location.altitude,
    status.time.jday,
  );
  applyPointing(j2000, status.view.fov);
  setDesktopSelection(status.selectioninfo ? await selectionIcrs() : null);

  return { status, view, j2000, meteors, online: true };
}

export function startSync(onState: (state: DesktopState | { online: false; error: string }) => void): () => void {
  let cancelled = false;

  const tick = async () => {
    try {
      const state = await pollDesktop();
      if (cancelled) {
        return;
      }
      onState(state);
    } catch (err) {
      if (!cancelled) {
        onState({ online: false, error: err instanceof Error ? err.message : "offline" });
      }
    }
  };

  void tick();
  const id = window.setInterval(() => void tick(), POLL_MS);
  return () => {
    cancelled = true;
    window.clearInterval(id);
  };
}

export function selectionTitle(html: string): string {
  const match = html.match(/<h2[^>]*>([\s\S]*?)<\/h2>/i);
  if (!match?.[1]) {
    return "";
  }
  return match[1].replace(/<[^>]+>/g, "").replace(/&mdash;/g, "—").trim();
}
