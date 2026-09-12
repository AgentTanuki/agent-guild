import { makeActions } from "./actions.js";

export function createAgentGuildPlugin(config) {
	return {
		name: "agent-guild",
		packageName: "elizaos-plugin-agent-guild",
		description:
			"Explicit endpoint observations and public passport verification; no automatic delegation or payment.",
		actions: makeActions(config),
	};
}

export default createAgentGuildPlugin();
