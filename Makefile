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

TP := live/trustplane
# framework integration environments (independently resolvable — crewai pins
# mcp~=1.26.0 while the mcp proxy tests 1.28.1, so they must NOT co-install)
TP_ENVS := core crewai langchain openai-agents mcp

.PHONY: all setup test test-json test-sqlite test-kdf contract verify-independent live-conformance clean \
	trustplane-test trustplane-conformance trustplane-conformance-live \
	$(addprefix trustplane-install-,$(TP_ENVS)) $(addprefix trustplane-test-,$(TP_ENVS))

all: setup test test-kdf contract verify-independent trustplane-test trustplane-conformance

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

# --- trust plane (live/trustplane) -------------------------------------------
# ONE reproducible clean command per supported integration:
#   make trustplane-install-crewai   → fresh venv resolving ONLY that extra
#   make trustplane-test-crewai      → its native adapter tests in that venv
trustplane-install-%:
	$(PY) -m venv $(TP)/.venv-$*
	$(TP)/.venv-$*/bin/pip install -q --upgrade pip
	$(TP)/.venv-$*/bin/pip install -q -r $(TP)/requirements/$*.txt pytest

trustplane-test-%: trustplane-install-%
	cd $(TP) && .venv-$*/bin/python -m pytest tests -q

# core trust-plane suite + conformance in the shared repo venv
trustplane-test: setup
	$(VPY) -m pip install -q -r $(TP)/requirements/core.txt pytest
	cd $(TP) && ../../$(VPY) -m pytest tests -q

trustplane-conformance: setup
	cd $(TP) && ../../$(VPY) -m pytest conformance/ -q

trustplane-conformance-live: setup
	cd $(TP) && ../../$(VPY) -m pytest conformance/ -q \
	  --issuer-base=https://agent-guild-5d5r.onrender.com --capability=hello

clean:
	rm -rf $(VENV) verifiers/node_modules verifiers/vector.json $(TP)/.venv-*
