import { IntegrationDefinition, z } from "@botpress/sdk";
import { integrationName } from "./package.json";

const check = z.enum([
  "endpoint_reachable",
  "protocol_handshake",
  "agent_card_resolves",
  "agent_card_signed",
  "payment_claim_holds",
  "independent_evidence",
]);
const nullableText = () => z.string().nullable();

export default new IntegrationDefinition({
  name: integrationName,
  version: "0.1.0",
  title: "Agent Guild Endpoint Observations",
  description:
    "Request structured observations for a selected public endpoint before making a separate connection decision.",
  readme: "hub.md",
  icon: "icon.svg",
  configuration: {
    schema: z
      .object({})
      .strict()
      .title("No configuration")
      .describe("No Guild key, account or endpoint configuration is required."),
  },
  actions: {
    observeEndpoint: {
      title: "Observe public endpoint",
      description:
        "Send only the selected public URL to Guild preflight. Returns six reported checks, unknowns and limits; does not connect tools or authorize delegation.",
      input: {
        schema: z
          .object({
            url: z
              .string()
              .min(1)
              .max(2048)
              .title("Public endpoint URL")
              .describe(
                "Exact public HTTP(S) URL selected for this observation. Do not include secrets or private resource identifiers.",
              ),
          })
          .strict()
          .title("Selected public endpoint")
          .describe(
            "The action sends this URL, and no conversation or credentials, to the fixed Guild service.",
          ),
      },
      output: {
        schema: z
          .object({
            status: z
              .enum(["observed", "rejected", "unavailable"])
              .title("Observation status")
              .describe(
                "Observed means a structurally consistent report was received, not that the endpoint is safe.",
              ),
            code: nullableText()
              .title("Failure code")
              .describe(
                "Fixed local failure code, or null for an observed report.",
              ),
            serviceOrigin: z
              .string()
              .title("Observation service")
              .describe("Fixed Guild service origin."),
            target: nullableText()
              .title("Exact observed target")
              .describe(
                "Original selected URL only when the response matched it exactly.",
              ),
            requestedAt: nullableText()
              .title("Request time")
              .describe(
                "Local UTC request timestamp; not a signed service timestamp.",
              ),
            completedAt: nullableText()
              .title("Response time")
              .describe(
                "Local UTC completion timestamp; later execution is not bound to this observation.",
              ),
            httpStatus: z
              .number()
              .int()
              .nullable()
              .title("HTTP status")
              .describe(
                "Successful observation service HTTP status, otherwise null.",
              ),
            responseBytes: z
              .number()
              .int()
              .nullable()
              .title("Decoded response bytes")
              .describe(
                "Bounded bytes read after native fetch decoding, otherwise null.",
              ),
            verdict: z
              .enum([
                "do_not_delegate",
                "delegate_with_caution",
                "no_failed_checks",
              ])
              .nullable()
              .title("Reported verdict")
              .describe(
                "Verified only for consistency with check statuses; not authorization or a safety guarantee.",
              ),
            checks: z
              .array(
                z.object({
                  check: check
                    .title("Check name")
                    .describe("Canonical observation check."),
                  status: z
                    .enum(["proven", "failed", "unknown"])
                    .title("Reported check status")
                    .describe(
                      "Service-reported status; not independent verification.",
                    ),
                }),
              )
              .max(6)
              .title("Six check statuses")
              .describe(
                "All six canonical checks on success; empty when no usable report is available.",
              ),
            failed: z
              .array(check)
              .title("Failed checks")
              .describe("Exactly the checks whose reported status is failed."),
            unknowns: z
              .array(check)
              .title("Unknown checks")
              .describe(
                "Unknown checks remain unknown and are excluded from the reported verdict.",
              ),
            scored: z
              .array(check)
              .title("Scored checks")
              .describe("Exactly the checks that are not unknown."),
            limitations: z
              .array(z.string())
              .title("Interpretation limits")
              .describe(
                "Fixed local limits; remote explanatory prose is not forwarded.",
              ),
          })
          .strict()
          .title("Endpoint observation")
          .describe(
            "Bounded structured evidence for a separate operator decision.",
          ),
      },
    },
  },
});
