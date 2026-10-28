// One-shot public model/AuthSource metadata only. Never requests credential
// values, raw auth storage, headers, prompts, messages, labels or traces.
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

export default function (api: ExtensionAPI) {
  api.on("before_agent_start", () => {
    throw new Error("TT7_METADATA_ONLY_NO_INFERENCE");
  });
  api.on("before_provider_request", () => {
    throw new Error("TT7_METADATA_ONLY_NO_INFERENCE");
  });
  api.on("session_start", async (_event, ctx) => {
    const model = ctx.models.current();
    const source = ctx.modelRegistry.authStorage.keys.source("openai-codex");
    let endpoint: { protocol: string; origin: string; pathname: string } | null = null;
    if (model?.baseUrl) {
      const url = new URL(model.baseUrl);
      endpoint = { protocol: url.protocol, origin: url.origin, pathname: url.pathname };
    }
    await Bun.write(Bun.stdout, JSON.stringify({
      type: "tt7_current_route_metadata",
      provider: model?.provider ?? null,
      id: model?.id ?? null,
      api: model?.api ?? null,
      endpoint,
      contextWindow: model?.contextWindow ?? null,
      maxTokens: model?.maxTokens ?? null,
      authSource: source ? { kind: source.kind, concrete: source.concrete } : null,
      credentialSourceMetadataOnly: true,
      inferencePermitted: false,
      rawAuthStoreRead: false,
      credentialValuesRead: false,
      rawHeadersRead: false,
    }) + "\n");
    ctx.shutdown();
  });
}
