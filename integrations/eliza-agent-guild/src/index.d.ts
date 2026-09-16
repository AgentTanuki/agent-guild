import type { Plugin } from "@elizaos/core";

export type JsonValue =
	| string
	| number
	| boolean
	| null
	| JsonValue[]
	| { [key: string]: JsonValue };
export interface AgentGuildConfig {
	/** Optional restriction. Omit for any caller-selected public HTTP(S) endpoint; [] disables endpoint checks. */
	allowedEndpoints?: string[];
	/** Optional additional issuer restriction. Caller still supplies expected issuer and subject independently. */
	expectedIssuerDid?: string;
	/** 1..604800; default 86400. Signed validFrom is the age reference. */
	maxPassportAgeSeconds?: number;
}
/** Snapshot explicit operator configuration; absent config reads only AGENT_GUILD_CONFIG. */
export function createAgentGuildPlugin(config?: AgentGuildConfig): Plugin;
declare const agentGuildPlugin: Plugin;
export default agentGuildPlugin;
