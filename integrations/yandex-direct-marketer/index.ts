import { readFileSync } from "node:fs";
import { homedir } from "node:os";

export default function (api: any) {
  const configPath = api.pluginConfig?.configFile ?? `${homedir()}/.config/yandex-direct-bot/marketer.json`;
  const config = JSON.parse(readFileSync(configPath, "utf8"));

  async function post(path: string, body: unknown, decision = false) {
    const response = await fetch(`http://127.0.0.1:8092${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${decision ? config.decision_key : config.api_key}` },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(180000),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error ?? `Backend HTTP ${response.status}`);
    return result;
  }

  api.registerCommand({
    name: "yd",
    description: "Решение по карточке маркетолога (без вызова модели)",
    acceptsArgs: true,
    requireAuth: true,
    async handler(ctx: any) {
      if (!ctx.isAuthorizedSender || ctx.channel !== "telegram" || String(ctx.senderId).replace(/^telegram:/, "") !== String(config.owner_id)) {
        return { text: "Решения доступны только владельцу в Telegram." };
      }
      const match = String(ctx.args ?? "").match(/^(approve|reject|defer|details|edit|done) ([a-f0-9]{12}) ([1-9][0-9]*)(?: ([1-9][0-9]*))?$/);
      if (!match || (match[4] && match[1] !== "details") || !Number.isSafeInteger(Number(match[3])) ||
          (match[4] && !Number.isSafeInteger(Number(match[4])))) {
        return { text: "Используйте кнопки на конкретной карточке предложения." };
      }
      try {
        const result = await post("/decision", {
          decision: match[1], id: match[2], revision: Number(match[3]),
          sender_id: String(config.owner_id), channel: "telegram",
          ...(match[4] ? { page: Number(match[4]) } : {}),
        }, true);
        return { text: result.text, ...(result.buttons?.length ? { channelData: { telegram: { buttons: result.buttons } } } : {}) };
      } catch (error) {
        return { text: `Решение не подтверждено: ${String(error)}` };
      }
    },
  });

  const definitions = [
    ["marketer_status", "/status", "Read autonomous analysis status and independent proposal states.", {}],
    ["marketer_context", "/context", "Read collected Direct/Metrika facts and proposal history, including run id. No live writes.", {}],
    ["marketer_proposal", "/proposal", "Read one proposal, exact action, evidence and version.", { id: { type: "string" } }],
    ["marketer_propose", "/propose", "Create ONE independently approvable card; never applies changes. Actions: add_negative, ad_text, strategy_value, advisory; product_feed_register, product_create, product_edit, product_ad_edit, product_moderate, product_launch, product_pause_old. Read docs/PRODUCT_CAMPAIGNS.md for strict schemas. No approval tool.", { proposal: { type: "object", additionalProperties: true } }],
    ["marketer_revise", "/revise", "Edit only the requested pending card. Creates a new version requiring fresh owner approval. Does not change other cards.", { id: { type: "string" }, revision: { type: "integer" }, proposal: { type: "object", additionalProperties: true } }],
    ["product_catalog", "/products/catalog", "Read and validate the owner's native auto-updating InSales YML URL. No Direct writes. Inventory is not proof of paid sales.", { feed_url: { type: "string" } }],
    ["product_feeds", "/products/feeds", "Read Direct feed processing statuses. Never register a duplicate feed.", {}],
    ["product_draft", "/products/draft", "Create a local product campaign draft, not a live campaign. Strict schema in docs/PRODUCT_CAMPAIGNS.md. Requires explicit purchase CPA, weekly budget, regions, category IDs and native feed URL.", { draft: { type: "object", additionalProperties: true } }],
    ["product_draft_revise", "/products/draft", "Revise one local product draft by id and revision. Old creation cards become stale. No live writes.", { id: { type: "string" }, revision: { type: "integer" }, draft: { type: "object", additionalProperties: true } }],
    ["product_get_draft", "/products/get_draft", "Read one product draft and its current revision.", { id: { type: "string" } }],
    ["product_inspect", "/products/inspect", "Read product campaign, ads, managed state and purchase billing check.", { campaign_id: { type: "integer" } }],
    ["product_candidates", "/products/candidates", "Read evidence-based replacement candidates. Unknown conversions are not zero. Preserve working campaigns; no automatic pause.", {}],
    ["product_operation", "/products/operation", "Read per-step operation journal for a proposal, including partial/uncertain results. Never retry uncertain writes.", { id: { type: "string" } }],
  ] as const;

  for (const [name, path, description, properties] of definitions) {
    api.registerTool({
      name, description,
      parameters: { type: "object", additionalProperties: false, properties, required: Object.keys(properties) },
      async execute(_id: string, params: unknown) {
        const result = await post(path, params);
        return { content: [{ type: "text", text: JSON.stringify(result) }] };
      },
    });
  }
  api.logger.info("yandex-direct-marketer: proposal tools and owner /yd decisions registered");
}
