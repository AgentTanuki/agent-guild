// Deterministic, seedable PRNG so the whole simulation is reproducible.
export class RNG {
  private state: number;
  constructor(seed = 0x9e3779b9) {
    this.state = seed >>> 0;
  }
  /** Uniform float in [0,1). mulberry32. */
  next(): number {
    this.state |= 0;
    this.state = (this.state + 0x6d2b79f5) | 0;
    let t = Math.imul(this.state ^ (this.state >>> 15), 1 | this.state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  }
  /** Uniform float in [min,max). */
  range(min: number, max: number): number {
    return min + (max - min) * this.next();
  }
  /** Integer in [0,n). */
  int(n: number): number {
    return Math.floor(this.next() * n);
  }
  /** Approx. standard normal via Box–Muller. */
  gauss(mean = 0, sd = 1): number {
    const u = Math.max(1e-9, this.next());
    const v = this.next();
    return mean + sd * Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
  }
  pick<T>(arr: T[]): T {
    return arr[this.int(arr.length)];
  }
  /** 32 deterministic bytes (for keypair seeds). */
  bytes32(): Uint8Array {
    const out = new Uint8Array(32);
    for (let i = 0; i < 32; i++) out[i] = this.int(256);
    return out;
  }
}

export const clamp01 = (x: number) => Math.max(0, Math.min(1, x));
