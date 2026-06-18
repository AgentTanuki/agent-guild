"""AgentRuntime: bridges Guild identity/discovery with in-process execution.

Identity, discovery, and attestation all go through the live Guild API. Task
execution is dispatched in-process via a registry mapping a Guild agent id to
its worker object. (In a full deployment, metadata['endpoint'] would be an HTTP
URL and dispatch would be a network call — the rest is identical.)
"""
from __future__ import annotations

from typing import Any, Callable

from agentguild import GuildClient, GuildIdentity
from workers import FactCheckAgent, SummariserAgent, WorkerProfile


class AgentRuntime:
    def __init__(self, client: GuildClient, admin_token: str | None = None):
        self.client = client
        self.admin_token = admin_token
        self.workers: dict[str, Any] = {}            # guild agent id -> worker object
        self.identities: dict[str, GuildIdentity] = {}

    def register_factchecker(self, profile: WorkerProfile, seed: bool = False) -> str:
        ident = self.client.register(
            name=profile.name, capabilities=[profile.capability],
            metadata=profile.metadata(), seed=seed, admin_token=self.admin_token,
        )
        self.workers[ident.id] = FactCheckAgent(profile)
        self.identities[ident.id] = ident
        return ident.id

    def register_summariser(self, profile: WorkerProfile) -> str:
        ident = self.client.register(
            name=profile.name, capabilities=[profile.capability], metadata=profile.metadata(),
        )
        self.workers[ident.id] = SummariserAgent(profile)
        self.identities[ident.id] = ident
        return ident.id

    def profile_of(self, agent_id: str) -> WorkerProfile:
        return self.workers[agent_id].profile

    def execute_factcheck(self, agent_id: str, claim: str, mock_label: bool | None = None):
        worker: FactCheckAgent = self.workers[agent_id]
        return worker.verify(claim, mock_label=mock_label)

    def dispatch(self, agent_id: str) -> Callable:
        return self.workers[agent_id]
