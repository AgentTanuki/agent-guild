# Agent Guild — reproducible one-command environment + test matrix.
#
#   make all          → venv + full matrix (JSON store, SQLite store, strict-KDF
#                       policy, contract conformance, independent VC verifiers)
#   make test         → both store backends
#   make contract     → regenerate contract.json + derived artifacts; fail on drift
#   make verify-independent → third-party VC verification (Node + Python)
#   make live-conformance   → probe the LIVE service against the contract
#
# Everything a clean checkout needs; CI (.github/workflows/ci.yml) runs the
# same targets.

PY ?= python3
VENV := .venv
VPY := $(VENV)/bin/python
GUILD := live/guild

.PHONY: all setup test test-json test-sqlite test-kdf contract verify-independent live-conformance clean

all: setup test test-kdf contract verify-independent

$(VENV)/bin/python:
	$(PY) -m venv $(VENV)
	$(VPY) -m pip install -q --upgrade pip
	$(VPY) -m pip install -q -r $(GUILD)/requirements.txt
	$(VPY) -m pip install -q rfc8785 base58   # independent-verifier deps

setup: $(VENV)/bin/python

test: test-json test-sqlite

test-json: setup
	cd $(GUILD) && GUILD_ALLOW_WEAK_KDF=1 GUILD_STORE=json ../../$(VPY) -m pytest tests -q

test-sqlite: setup
	cd $(GUILD) && GUILD_ALLOW_WEAK_KDF=1 GUILD_STORE=sqlite ../../$(VPY) -m pytest tests -q

# strict production KDF policy (hashed credentials at full PBKDF2 cost) on the
# credential suites — the rest of the matrix runs with the fast test KDF.
test-kdf: setup
	cd $(GUILD) && ../../$(VPY) -m pytest tests/test_kdf_policy.py \
	  tests/test_credential_hardening.py tests/test_credential_lifecycle.py -q

contract: setup
	cd $(GUILD) && ../../$(VPY) contract/generate.py
	git diff --exit-code $(GUILD)/contract/contract.json server.json docs/INTERFACE.md \
	  || (echo "CONTRACT DRIFT: commit the regenerated contract artifacts" && exit 1)

verify-independent: setup
	cd $(GUILD) && ../../$(VPY) ../../verifiers/gen_vector.py ../../verifiers/vector.json
	$(VPY) verifiers/verify_python_independent.py verifiers/vector.json
	cd verifiers && (test -d node_modules || npm install --no-fund --no-audit) \
	  && node verify_node_digitalbazaar.mjs vector.json

live-conformance: setup
	$(VPY) live/scripts/live_contract_probe.py

clean:
	rm -rf $(VENV) verifiers/node_modules verifiers/vector.json
