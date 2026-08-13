# Trust Circuit

An original, single-player browser arcade strategy game about machine-message trust. Inspect each inbound packet and decide whether to relay it into the trust chain or quarantine it before it can contaminate the network.

## Play

Public demo: `https://codex-autonomous-worker.rwdburley.chatgpt.site/trust-circuit`

20-second gameplay GIF: `https://codex-autonomous-worker.rwdburley.chatgpt.site/trust-circuit-gameplay.gif`

No account, install, backend, wallet, API key, or paid service is required. The game runs entirely in the browser. The only persistent value is the local high score stored on the current device.

## Controls

| Action | Keyboard | Touch / pointer |
|---|---|---|
| Relay a clean packet | `R` or `→` | **Relay** button |
| Quarantine a bad packet | `Q` or `←` | **Quarantine** button |
| Start / restart | `Enter` or `Space` | **Start Circuit** / **Route Again** |

## How to play

A packet is safe to relay only when all three checks pass:

1. `SIGNATURE` is `VALID`.
2. `NONCE` is `FRESH`.
3. `PROOF TTL` is greater than zero.

Quarantine a packet if any one of those checks fails. Correct routing extends the trust chain and awards a speed and streak bonus. A wrong decision removes one integrity point; three errors collapse the circuit. A complete run lasts 150 seconds, with hostile packet frequency rising in four pressure stages.

## Progress and scoring

- Correct decision: 100 base points, plus up to 400 chain points and a TTL speed bonus.
- Incorrect decision: 175-point penalty and one integrity point.
- Run objectives: build a 10-packet chain, reach 3,000 points, and process 12 packets without an error.
- End ranks: Packet Initiate, Proof Operator, Trust Architect, and Sovereign Router.

## Design notes

The core interaction translates three real machine-communication checks into a one-second decision loop: signature authenticity, replay protection, and proof expiry. It is deliberately readable before it is difficult. The packet card always exposes the same four fields; challenge comes from rising frequency of invalid combinations and the value of maintaining a long trust chain.

The interface is built from typography, CSS circuit geometry, and accessible HTML controls. It supports current desktop Chrome, Safari, and Firefox and collapses into a touch-friendly single-column layout on smaller screens. Motion is limited to brief feedback; gameplay never depends on animation or sound.

## Run locally

From this repository's site directory, install its existing dependencies and run the existing development command. Open `/trust-circuit` in a modern browser. No environment variables or external services are needed for the game.

## Submission contents

- Playable demo route: `app/trust-circuit/`
- Game styles: `app/trust-circuit/trust-circuit.module.css`
- Social preview: `public/trust-circuit-og.png`
- 20-second gameplay GIF: `public/trust-circuit-gameplay.gif`
- This controls, run, and design guide: `TRUST_CIRCUIT.md`
