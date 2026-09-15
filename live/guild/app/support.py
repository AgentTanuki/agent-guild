"""Optional support contribution, separate from metered service purchases."""
from html import escape

STRIPE_URL = "https://buy.stripe.com/4gM28rcidcv95mY4vu0Ny00"
LABEL = "Support Agent Guild — contribute £1"
DESCRIPTION = "A voluntary contribution to support Agent Guild. No service or API credits are included."


def contribution() -> dict:
    return {
        "purpose": "voluntary_support",
        "label": LABEL,
        "checkout_url": STRIPE_URL,
        "provider": "Stripe",
        "checkout_type": "hosted",
        "amount": {"currency": "GBP", "value": "1.00"},
        "description": DESCRIPTION,
        "service_included": False,
        "api_credits_included": False,
    }


def markdown() -> str:
    return f"## Optional support\n\n[{LABEL}]({STRIPE_URL})\n\n{DESCRIPTION}\n\n"


def html() -> str:
    return (f'<h2>Optional support</h2><p class=k><a href="{STRIPE_URL}">'
            f'{escape(LABEL)}</a><br>{escape(DESCRIPTION)}</p>')
