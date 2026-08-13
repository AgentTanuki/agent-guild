import type { Metadata } from "next";
import TrustCircuitGame from "./TrustCircuitGame";

export const metadata: Metadata = {
  title: "Trust Circuit — a machine-message arcade game",
  description:
    "Route valid signed packets, quarantine forged proofs, preserve nonce order, and build a trust chain before the clock expires.",
  alternates: { canonical: "/trust-circuit" },
  openGraph: {
    title: "Trust Circuit",
    description: "Route proof. Build trust. Beat the clock.",
    images: [{ url: "/trust-circuit-og.png", alt: "Trust Circuit game key art" }],
  },
  twitter: {
    card: "summary_large_image",
    title: "Trust Circuit",
    description: "Route proof. Build trust. Beat the clock.",
    images: ["/trust-circuit-og.png"],
  },
};

export default function TrustCircuitPage() {
  return <TrustCircuitGame />;
}
