import * as sdk from "@botpress/sdk";
import * as bp from ".botpress";
import { observeEndpoint } from "./observation";

export default new bp.Integration({
  register: async ({ ctx }) => {
    if (
      !ctx.configuration ||
      typeof ctx.configuration !== "object" ||
      Array.isArray(ctx.configuration) ||
      Object.keys(ctx.configuration).length !== 0
    ) {
      throw new sdk.RuntimeError(
        "This integration has no configuration fields. Remove extra values and save again.",
      );
    }
  },
  unregister: async () => {},
  actions: {
    observeEndpoint: async ({ input }) => observeEndpoint(input),
  },
  channels: {},
  handler: async () => {},
});
