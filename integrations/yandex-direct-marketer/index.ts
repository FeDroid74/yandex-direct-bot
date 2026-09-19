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
      const match = String(ctx.args ?? "").match(/^(approve|reject|defer|details|edit|done) ([a-f0-9]{12}) ([1-9][0-9]*)$/);
      if (!match) return { text: "Используйте кнопки на конкретной карточке предложения." };
      try {
        const result = await post("/decision", {
          decision: match[1], id: match[2], revision: Number(match[3]),
          sender_id: String(config.owner_id), channel: "telegram",
        }, true);
        return { text: result.text };
      } catch (error) {
        return { text: `Решение не подтверждено: ${String(error)}` };
      }
    },
  });

  const definitions = [
    ["marketer_status", "/status", "Read autonomous analysis status and independent proposal states.", {}],
    ["marketer_context", "/context", "Read collected Direct/Metrika facts and proposal history, including run id. No live writes.", {}],
    ["marketer_proposal", "/proposal", "Read one proposal, exact action, evidence and version.", { id: { type: "string" } }],
    ["marketer_propose", "/propose", "Create ONE independently approvable card; never applies changes. Supported actions: add_negative, ad_text, strategy_value, advisory. Use current collected context and explicit facts.", { proposal: { type: "object", additionalProperties: true } }],
    ["marketer_revise", "/revise", "Edit only the requested pending card. Creates a new version requiring fresh owner approval. Does not change other cards.", { id: { type: "string" }, revision: { type: "integer" }, proposal: { type: "object", additionalProperties: true } }],
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
