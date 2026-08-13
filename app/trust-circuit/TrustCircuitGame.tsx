"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import styles from "./trust-circuit.module.css";

type Packet = {
  id: number;
  from: string;
  to: string;
  signature: "VALID" | "INVALID";
  nonce: "FRESH" | "REPLAY";
  ttl: number;
  threat: "clean" | "forged signature" | "replayed nonce" | "expired proof";
};

type Resolution = "relay" | "quarantine";
type Phase = "briefing" | "running" | "finished";

const RUN_SECONDS = 150;
const NODES = ["A7", "C2", "D9", "F4", "K8", "M3", "Q6", "V1"];
const INITIAL_PACKET: Packet = {
  id: 1,
  from: "NODE_A7",
  to: "NODE_F4",
  signature: "VALID",
  nonce: "FRESH",
  ttl: 12,
  threat: "clean",
};

function makePacket(id: number, pressure: number): Packet {
  const dangerRate = Math.min(0.72, 0.35 + pressure * 0.08);
  const dangerous = Math.random() < dangerRate;
  const mode = dangerous ? 1 + Math.floor(Math.random() * 3) : 0;
  const from = NODES[Math.floor(Math.random() * NODES.length)];
  let to = NODES[Math.floor(Math.random() * NODES.length)];
  if (to === from) to = NODES[(NODES.indexOf(from) + 3) % NODES.length];
  const ttl = mode === 3 ? 0 : Math.max(1, 14 - pressure - Math.floor(Math.random() * 7));

  return {
    id,
    from: `NODE_${from}`,
    to: `NODE_${to}`,
    signature: mode === 1 ? "INVALID" : "VALID",
    nonce: mode === 2 ? "REPLAY" : "FRESH",
    ttl,
    threat:
      mode === 1
        ? "forged signature"
        : mode === 2
          ? "replayed nonce"
          : mode === 3
            ? "expired proof"
            : "clean",
  };
}

function shortHash(id: number) {
  return `${(id * 2654435761 >>> 0).toString(16).padStart(8, "0")}…${(
    id * 97 + 4093
  ).toString(16).slice(-4)}`;
}

export default function TrustCircuitGame() {
  const [phase, setPhase] = useState<Phase>("briefing");
  const [seconds, setSeconds] = useState(RUN_SECONDS);
  const [packet, setPacket] = useState(INITIAL_PACKET);
  const [score, setScore] = useState(0);
  const [chain, setChain] = useState(0);
  const [bestChain, setBestChain] = useState(0);
  const [integrity, setIntegrity] = useState(3);
  const [processed, setProcessed] = useState(0);
  const [lastResult, setLastResult] = useState("Awaiting operator input");
  const [flash, setFlash] = useState<"good" | "bad" | null>(null);
  const [highScore, setHighScore] = useState(0);
  const packetId = useRef(1);
  const locked = useRef(false);
  const scoreRef = useRef(0);

  useEffect(() => {
    const saved = Number(window.localStorage.getItem("trust-circuit-high-score") || 0);
    if (Number.isFinite(saved)) setHighScore(saved);
  }, []);

  const pressure = Math.min(3, Math.floor((RUN_SECONDS - seconds) / 38));
  const validPacket =
    packet.signature === "VALID" && packet.nonce === "FRESH" && packet.ttl > 0;

  const finish = useCallback(() => {
    setPhase("finished");
    setHighScore((previous) => {
      const next = Math.max(previous, scoreRef.current);
      window.localStorage.setItem("trust-circuit-high-score", String(next));
      return next;
    });
  }, []);

  useEffect(() => {
    if (phase !== "running") return;
    const timer = window.setInterval(() => {
      setSeconds((current) => {
        if (current <= 1) {
          window.clearInterval(timer);
          window.setTimeout(finish, 0);
          return 0;
        }
        return current - 1;
      });
    }, 1000);
    return () => window.clearInterval(timer);
  }, [finish, phase]);

  const resolve = useCallback(
    (choice: Resolution) => {
      if (phase !== "running" || locked.current) return;
      locked.current = true;
      const correct = (choice === "relay") === validPacket;
      const nextChain = correct ? chain + 1 : 0;
      const speedBonus = Math.max(0, packet.ttl) * 4;
      const delta = correct ? 100 + Math.min(400, nextChain * 20) + speedBonus : -175;
      const action = choice === "relay" ? "RELAYED" : "QUARANTINED";

      setScore((value) => {
        const next = Math.max(0, value + delta);
        scoreRef.current = next;
        return next;
      });
      setChain(nextChain);
      setBestChain((value) => Math.max(value, nextChain));
      setProcessed((value) => value + 1);
      setFlash(correct ? "good" : "bad");
      setLastResult(
        correct
          ? `${action} · ${validPacket ? "proof accepted" : packet.threat + " contained"} · +${delta}`
          : `${action} IN ERROR · packet was ${validPacket ? "clean" : packet.threat} · integrity -1`,
      );

      if (!correct) {
        setIntegrity((value) => {
          const next = value - 1;
          if (next <= 0) window.setTimeout(finish, 350);
          return Math.max(0, next);
        });
      }

      window.setTimeout(() => {
        packetId.current += 1;
        setPacket(makePacket(packetId.current, pressure));
        setFlash(null);
        locked.current = false;
      }, 260);
    },
    [chain, finish, packet, phase, pressure, validPacket],
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.repeat) return;
      if (event.key.toLowerCase() === "r" || event.key === "ArrowRight") resolve("relay");
      if (event.key.toLowerCase() === "q" || event.key === "ArrowLeft") resolve("quarantine");
      if ((event.key === "Enter" || event.key === " ") && phase !== "running") start();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const start = () => {
    packetId.current = 1;
    locked.current = false;
    setSeconds(RUN_SECONDS);
    setPacket(makePacket(1, 0));
    setScore(0);
    scoreRef.current = 0;
    setChain(0);
    setBestChain(0);
    setIntegrity(3);
    setProcessed(0);
    setLastResult("Circuit live · inspect the first proof");
    setFlash(null);
    setPhase("running");
  };

  const rank = useMemo(() => {
    if (score >= 9000) return "SOVEREIGN ROUTER";
    if (score >= 6000) return "TRUST ARCHITECT";
    if (score >= 3000) return "PROOF OPERATOR";
    return "PACKET INITIATE";
  }, [score]);

  return (
    <main className={styles.shell}>
      <div className={styles.grid} aria-hidden="true" />
      <header className={styles.topbar}>
        <a href="/" className={styles.brand}><span>TC</span> TRUST CIRCUIT</a>
        <div className={styles.topStats}>
          <span>LOCAL HIGH <strong>{highScore.toLocaleString()}</strong></span>
          <span className={styles.liveDot}>OFFLINE · NO ACCOUNT</span>
        </div>
      </header>

      <section className={styles.game} aria-label="Trust Circuit game">
        <aside className={styles.statusRail}>
          <div><small>TIME</small><strong>{Math.floor(seconds / 60)}:{String(seconds % 60).padStart(2, "0")}</strong></div>
          <div><small>SCORE</small><strong>{score.toLocaleString()}</strong></div>
          <div><small>CHAIN</small><strong>{chain}</strong></div>
          <div><small>INTEGRITY</small><strong className={styles.hearts}>{"◆".repeat(integrity)}{"◇".repeat(3 - integrity)}</strong></div>
          <div><small>PRESSURE</small><strong>P{pressure + 1}</strong></div>
        </aside>

        <div className={`${styles.board} ${flash ? styles[flash] : ""}`}>
          <div className={styles.network} aria-hidden="true">
            <span className={styles.origin}>ORIGIN</span>
            <i className={styles.lineIn} />
            <span className={styles.switch}>⌁</span>
            <i className={styles.lineGood} />
            <i className={styles.lineBad} />
            <span className={styles.relayNode}>RELAY</span>
            <span className={styles.quarantineNode}>LOCK</span>
          </div>

          <article className={styles.packet} aria-live="polite">
            <div className={styles.packetHead}>
              <span>INBOUND // {String(packet.id).padStart(4, "0")}</span>
              <span>TTL {String(packet.ttl).padStart(2, "0")}</span>
            </div>
            <div className={styles.route}><b>{packet.from}</b><span>→</span><b>{packet.to}</b></div>
            <dl>
              <div><dt>SIGNATURE</dt><dd className={packet.signature === "VALID" ? styles.pass : styles.fail}>{packet.signature}</dd></div>
              <div><dt>NONCE</dt><dd className={packet.nonce === "FRESH" ? styles.pass : styles.fail}>{packet.nonce}</dd></div>
              <div><dt>PROOF TTL</dt><dd className={packet.ttl > 0 ? styles.pass : styles.fail}>{packet.ttl > 0 ? `${packet.ttl}s` : "EXPIRED"}</dd></div>
              <div><dt>HASH</dt><dd>{shortHash(packet.id)}</dd></div>
            </dl>
          </article>

          {phase === "running" && (
            <div className={styles.controls}>
              <button onClick={() => resolve("quarantine")} className={styles.quarantine}>
                <kbd>Q</kbd><span>QUARANTINE<small>contain invalid proof</small></span>
              </button>
              <button onClick={() => resolve("relay")} className={styles.relay}>
                <span>RELAY<small>extend trust chain</small></span><kbd>R</kbd>
              </button>
            </div>
          )}

          <div className={styles.resultLine}>{lastResult}</div>

          {phase !== "running" && (
            <div className={styles.overlay}>
              {phase === "briefing" ? (
                <>
                  <p className={styles.overline}>MACHINE MESSAGE CONTROL // BRIEFING</p>
                  <h1>Route proof.<br /><em>Build trust.</em></h1>
                  <p>Relay a packet only when its signature is valid, nonce is fresh, and TTL is alive. Quarantine everything else. Three routing errors collapse the circuit.</p>
                  <div className={styles.rules}><span><b>Q / ←</b> quarantine</span><span><b>R / →</b> relay</span><span><b>150s</b> full run</span></div>
                  <button className={styles.start} onClick={start}>START CIRCUIT <span>↗</span></button>
                </>
              ) : (
                <>
                  <p className={styles.overline}>RUN COMPLETE // {integrity > 0 ? "CIRCUIT HELD" : "CIRCUIT COLLAPSED"}</p>
                  <h1>{score.toLocaleString()}<br /><em>{rank}</em></h1>
                  <p>{processed} packets processed · best trust chain {bestChain} · integrity {integrity}/3. Scores stay on this device only.</p>
                  <button className={styles.start} onClick={start}>ROUTE AGAIN <span>↻</span></button>
                </>
              )}
            </div>
          )}
        </div>

        <aside className={styles.missionRail}>
          <p className={styles.railTitle}>RUN OBJECTIVES</p>
          <div className={chain >= 10 ? styles.done : ""}><span>01</span><p>Build a 10-packet trust chain</p></div>
          <div className={score >= 3000 ? styles.done : ""}><span>02</span><p>Reach 3,000 points</p></div>
          <div className={integrity === 3 && processed >= 12 ? styles.done : ""}><span>03</span><p>Process 12 with perfect integrity</p></div>
          <footer><span>PROCESSED {processed}</span><span>BEST {bestChain}</span></footer>
        </aside>
      </section>

      <footer className={styles.siteFooter}>
        <span>ORIGINAL SINGLE-PLAYER BROWSER GAME</span>
        <span>KEYBOARD + TOUCH · NO LOGIN · NO BACKEND</span>
        <a href="https://github.com/AgentTanuki" rel="noreferrer">SOURCE ↗</a>
      </footer>
    </main>
  );
}
