export const STELLARIUM_PROXY = "/api/stellarium";

export type Vec3 = [number, number, number];

export type StellariumLocation = {
  name: string;
  planet: string;
  latitude: number;
  longitude: number;
  altitude: number;
  region?: string;
  state?: string;
};

export type StellariumTime = {
  jday: number;
  utc: string;
  local: string;
  timeZone: string;
  isTimeNow: boolean;
  timerate: number;
};

export type StellariumStatus = {
  location: StellariumLocation;
  time: StellariumTime;
  selectioninfo: string;
  view: { fov: number };
};

export type StellariumView = {
  j2000?: string;
  jNow?: string;
  altAz?: string;
};

export type ObjectInfo = {
  found?: boolean;
  name?: string;
  "localized-name"?: string;
  raJ2000?: number;
  decJ2000?: number;
  ra?: number;
  dec?: number;
  type?: string;
  "object-type"?: string;
};

export type SkyMarker = {
  name: string;
  raDeg: number;
  decDeg: number;
  kind: "selection" | "sun" | "moon" | "planet";
};

export async function stellariumGet<T>(path: string): Promise<T> {
  const res = await fetch(`${STELLARIUM_PROXY}/${path}`, { cache: "no-store" });
  if (!res.ok) {
    throw new Error(`${path} ${res.status}`);
  }
  const type = res.headers.get("content-type") ?? "";
  if (type.includes("json")) {
    return (await res.json()) as T;
  }
  return (await res.text()) as T;
}

export function parseVec3(raw: string | undefined): Vec3 | null {
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
    return normalize([x, y, z]);
  } catch {
    return null;
  }
}

export function normalize(v: Vec3): Vec3 {
  const n = Math.hypot(v[0], v[1], v[2]) || 1;
  return [v[0] / n, v[1] / n, v[2] / n];
}

export function dot(a: Vec3, b: Vec3): number {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

export function cross(a: Vec3, b: Vec3): Vec3 {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}

export function raDecToVec(raDeg: number, decDeg: number): Vec3 {
  const ra = (raDeg * Math.PI) / 180;
  const dec = (decDeg * Math.PI) / 180;
  const c = Math.cos(dec);
  return [c * Math.cos(ra), c * Math.sin(ra), Math.sin(dec)];
}

export function vecToRaDec(v: Vec3): { raDeg: number; decDeg: number } {
  const n = normalize(v);
  let raDeg = (Math.atan2(n[1], n[0]) * 180) / Math.PI;
  if (raDeg < 0) {
    raDeg += 360;
  }
  const decDeg = (Math.asin(Math.max(-1, Math.min(1, n[2]))) * 180) / Math.PI;
  return { raDeg, decDeg };
}

/** Stellarium altAz: x=south, y=east, z=up. Az' from south toward east. */
export function vecToAltAz(v: Vec3): { azDeg: number; altDeg: number } {
  const n = normalize(v);
  const altDeg = (Math.asin(Math.max(-1, Math.min(1, n[2]))) * 180) / Math.PI;
  const azPrime = (Math.atan2(n[1], n[0]) * 180) / Math.PI;
  const azDeg = (((180 - azPrime) % 360) + 360) % 360;
  return { azDeg, altDeg };
}

export function slerp(a: Vec3, b: Vec3, t: number): Vec3 {
  const d = Math.max(-1, Math.min(1, dot(a, b)));
  const omega = Math.acos(d);
  if (omega < 1e-5) {
    return normalize([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]);
  }
  const sinO = Math.sin(omega);
  const s0 = Math.sin((1 - t) * omega) / sinO;
  const s1 = Math.sin(t * omega) / sinO;
  return normalize([a[0] * s0 + b[0] * s1, a[1] * s0 + b[1] * s1, a[2] * s0 + b[2] * s1]);
}

export function equatorialBasis(center: Vec3): { east: Vec3; north: Vec3 } {
  const z: Vec3 = [0, 0, 1];
  let north = [z[0] - dot(z, center) * center[0], z[1] - dot(z, center) * center[1], z[2] - dot(z, center) * center[2]] as Vec3;
  if (Math.hypot(north[0], north[1], north[2]) < 1e-6) {
    north = [-center[0], -center[1], 0];
  }
  north = normalize(north);
  const east = normalize(cross(north, center));
  return { east, north };
}

export function formatRaHms(raDeg: number): string {
  const hours = (((raDeg / 15) % 24) + 24) % 24;
  const h = Math.floor(hours);
  const mFloat = (hours - h) * 60;
  const m = Math.floor(mFloat);
  const s = Math.round((mFloat - m) * 60);
  const ss = s === 60 ? 0 : s;
  const mm = s === 60 ? m + 1 : m;
  const hh = mm === 60 ? h + 1 : h;
  const pad = (n: number) => String(n % 24).padStart(2, "0");
  return `${pad(hh % 24)}h ${String(mm % 60).padStart(2, "0")}m ${String(ss).padStart(2, "0")}s`;
}

export function formatDecDms(decDeg: number): string {
  const sign = decDeg < 0 ? "−" : "+";
  const abs = Math.abs(decDeg);
  const d = Math.floor(abs);
  const mFloat = (abs - d) * 60;
  const m = Math.floor(mFloat);
  const s = Math.round((mFloat - m) * 60);
  const ss = s === 60 ? 0 : s;
  const mm = s === 60 ? m + 1 : m;
  const dd = mm === 60 ? d + 1 : d;
  return `${sign}${String(dd).padStart(2, "0")}° ${String(mm % 60).padStart(2, "0")}′ ${String(ss).padStart(2, "0")}″`;
}

export function selectionTitle(html: string): string {
  const match = html.match(/<h2[^>]*>([\s\S]*?)<\/h2>/i);
  if (!match?.[1]) {
    return "";
  }
  return match[1].replace(/<[^>]+>/g, "").replace(/&mdash;/g, "—").trim();
}

export function objectMarker(info: ObjectInfo, kind: SkyMarker["kind"]): SkyMarker | null {
  const ra = info.raJ2000 ?? info.ra;
  const dec = info.decJ2000 ?? info.dec;
  const name = info["localized-name"] || info.name;
  if (ra == null || dec == null || !name) {
    return null;
  }
  return { name, raDeg: ra, decDeg: dec, kind };
}

export const SOLAR_SYSTEM = [
  { name: "Sun", kind: "sun" as const },
  { name: "Moon", kind: "moon" as const },
  { name: "Mercury", kind: "planet" as const },
  { name: "Venus", kind: "planet" as const },
  { name: "Mars", kind: "planet" as const },
  { name: "Jupiter", kind: "planet" as const },
  { name: "Saturn", kind: "planet" as const },
] as const;
