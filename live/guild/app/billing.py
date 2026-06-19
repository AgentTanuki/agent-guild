"""Credit metering + a Stripe-ready top-up adapter.

The economic model: **writes are free, reads are paid.** Registering, attesting,
and posting task receipts grow the graph and must never be taxed. The value an
agent extracts — "who is the safest agent for this job?" — is metered.

Billing is prepaid credits, not per-call card charges (a $0.001 lookup cannot be
a Stripe transaction — the fee would dwarf it). An account holds a credit balance
and draws it down per paid call; balances are topped up in bulk.

Enforcement is gradual and controlled by environment variables so the open dev
service and the test suite keep working unchanged:

  * default                       paid reads are FREE unless you present a billing
                                  key (then you are charged) — a soft launch.
  * GUILD_BILLING_ENFORCED=1      paid reads REQUIRE a funded billing key (402).

Pricing is in **credits**; 1 credit = $0.001 (so a best-agent lookup = $0.01).
"""
from __future__ import annotations

import os

CREDIT_USD = 0.001          # 1 credit = one tenth of a cent
FREE_CREDITS = 100          # new account starts with $0.10 of free lookups
TRIAL_CREDITS = 500         # human-free trial grant: $0.50 of lookups to evaluate

# Per-endpoint price in credits. Writes are absent here = free.
PRICING: dict[str, int] = {
    "best_agent": 10,       # GET /search   — discovery, the headline product
    "reputation": 5,        # GET /agents/{id}/reputation
    "evidence": 5,          # GET /agents/{id}/evidence
    "risk_score": 10,       # GET /agents/{id}/risk-score
    "fraud_check": 5,       # GET /agents/{id}/flags and /flags
}


class InsufficientCredits(Exception):
    def __init__(self, balance: int, cost: int):
        self.balance = balance
        self.cost = cost
        super().__init__(f"insufficient credits: balance {balance}, need {cost}")


class UnknownAccount(Exception):
    pass


def billing_enforced() -> bool:
    return os.environ.get("GUILD_BILLING_ENFORCED", "") == "1"


def stripe_configured() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY"))


def dev_topup_token() -> str:
    """A shared secret that lets you mint credits without Stripe, for testing /
    private pilots. If unset, dev top-ups are open (fine for local only)."""
    return os.environ.get("GUILD_BILLING_DEV_TOKEN", "")


# --- Stripe adapter ---------------------------------------------------------
# Real Stripe is optional and never imported unless keys are present, so the
# service runs with zero payment dependencies until you decide to go live.
def create_checkout_session(account_key: str, credits: int, success_url: str,
                            cancel_url: str) -> dict:
    """Return a Stripe Checkout session for buying `credits`. Raises if Stripe
    is not configured — the caller falls back to the dev top-up path."""
    if not stripe_configured():
        raise RuntimeError("Stripe is not configured (set STRIPE_SECRET_KEY)")
    import stripe  # imported lazily; only a dependency if you actually use it
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    usd = round(credits * CREDIT_USD, 2)
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": f"Agent Guild — {credits} lookup credits"},
                "unit_amount": max(50, int(usd * 100)),  # cents; Stripe min ~$0.50
            },
            "quantity": 1,
        }],
        metadata={"account_key": account_key, "credits": str(credits)},
        success_url=success_url,
        cancel_url=cancel_url,
    )
    return {"checkout_url": session.url, "session_id": session.id, "usd": usd}


def verify_webhook(payload: bytes, sig_header: str) -> dict:
    """Verify a Stripe webhook and return the event. Raises if not configured."""
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        raise RuntimeError("Stripe webhook secret not configured")
    import stripe
    return stripe.Webhook.construct_event(payload, sig_header, secret)
