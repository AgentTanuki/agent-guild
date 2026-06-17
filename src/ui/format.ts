import type { Archetype } from "../lib/types";

export const ARCHETYPE_COLOR: Record<Archetype, string> = {
  honest: "#34d399",
  newcomer: "#5b8cff",
  incompetent: "#8a93a6",
  colluder: "#fbbf24",
  sybil: "#f87171",
};

export const ARCHETYPE_LABEL: Record<Archetype, string> = {
  honest: "Honest",
  newcomer: "Newcomer",
  incompetent: "Incompetent",
  colluder: "Colluder",
  sybil: "Sybil",
};

/** Trust score → colour (red → amber → green). */
export function trustColor(trust: number): string {
  if (trust >= 65) return "#34d399";
  if (trust >= 45) return "#a3d977";
  if (trust >= 30) return "#fbbf24";
  return "#f87171";
}

export function shortDid(did: string, n = 12): string {
  const id = did.replace("did:key:", "");
  return `did:key:${id.slice(0, n)}…${id.slice(-4)}`;
}

export function pct(x: number): string {
  return `${Math.round(x * 100)}%`;
}
