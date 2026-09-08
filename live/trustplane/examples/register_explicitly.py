"""Register an agent with the Guild — an EXPLICIT, caller-requested step.
Registration is optional: preflight, passports and quotes need no identity.

    python register_explicitly.py <name> <capability> [<capability> ...] [--base URL]
                                  [--principal org:you] [--public-key <ed25519 hex>]

The response includes a one-time `api_key` for custodial identities. This
program prints everything EXCEPT that key, and stores nothing. Keep the key
yourself and pass it as GuildClient(api_key=...) when you want authenticated
calls; without --public-key the Guild holds the signing key (custodial).
"""
import json
import sys

from agentguild_trustplane import DEFAULT_BASE, GuildClient, GuildError


def main(argv: list[str]) -> int:
    args = argv[1:]
    base, principal, public_key = DEFAULT_BASE, None, None
    positional: list[str] = []
    while args:
        a = args.pop(0)
        if a == "--base":
            base = args.pop(0)
        elif a == "--principal":
            principal = args.pop(0)
        elif a == "--public-key":
            public_key = args.pop(0)
        else:
            positional.append(a)
    if len(positional) < 2:
        print(__doc__)
        return 2
    name, capabilities = positional[0], positional[1:]
    client = GuildClient(base)
    try:
        resp = client.register(name, capabilities, principal=principal, public_key=public_key)
    except GuildError as e:
        print(f"registration failed: {e}")
        return 1
    shown = {k: v for k, v in resp.items() if k != "api_key"}
    print(json.dumps(shown, indent=1))
    if resp.get("api_key"):
        print("\nA one-time api_key was returned and is NOT shown or saved by this program;"
              " read it from the response object in your own code.")
    print(f"\nNext: python verify_passport.py {resp['id']} {base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
