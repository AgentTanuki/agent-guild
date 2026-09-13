"""Deterministic native Action execution. No planner, provider or production call.

Working principle: real use_actions -> ActionRegistry/ActionDispatcher -> actual
async Guild handlers -> fixed Guild HTTP boundary mapped to a local fixture.
Outer ActionResult success means execution completed; inspect the business result.
"""

import asyncio
import json

from agently import Agently

from .actions import ACTION_IDS, GuildActions
from .local_fixture import TARGET, LocalFixture, observed, passport_request, verification


async def run():
    with LocalFixture() as fixture:
        agent = Agently.create_agent()
        agent.use_actions(GuildActions(transport_factory=fixture.transport))
        fixture.reply(observed())
        endpoint = await agent.action.async_execute_action(
            ACTION_IDS[0], {"request": {"url": TARGET}}, source_protocol="structured_plan"
        )
        fixture.reply(verification(valid=False))
        passport = await agent.action.async_execute_action(
            ACTION_IDS[1], {"request": passport_request()}, source_protocol="direct"
        )
        summary = {
            "runtime": "Real Agently Actions; local HTTP fixtures; no model planning or independent crypto verification",
            "endpoint_action_status": endpoint["status"],
            "endpoint_observation_status": endpoint["data"]["status"],
            "endpoint_check_count": len(endpoint["data"]["checks"]),
            "passport_action_status": passport["status"],
            "passport_business_status": passport["data"]["status"],
            "passport_verified": passport["data"]["verified"],
            "http_requests": len(fixture.requests),
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return summary


if __name__ == "__main__":
    asyncio.run(run())

# Expected key output from a real local run (fixture verification is deliberately negative):
# {"endpoint_action_status": "success", "endpoint_observation_status": "observed",
#  "endpoint_check_count": 6, "passport_action_status": "success",
#  "passport_business_status": "completed", "passport_verified": false, "http_requests": 2}
