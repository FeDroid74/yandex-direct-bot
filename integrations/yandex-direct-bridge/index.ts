type JsonRecord = Record<string, unknown>;

async function postJson(baseUrl: string, path: string, body: unknown) {
  const response = await fetch(`${baseUrl}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(120000),
  });

  const raw = await response.text();

  let data: unknown = {};
  try {
    data = raw ? JSON.parse(raw) : {};
  } catch {
    data = { raw };
  }

  if (!response.ok) {
    throw new Error(
      `HTTP ${response.status} ${response.statusText}: ${JSON.stringify(data)}`
    );
  }

  return data;
}

export default function (api: any) {
  const baseUrl =
    (api.pluginConfig as { baseUrl?: string } | undefined)?.baseUrl ??
    "http://127.0.0.1:8091";

  api.registerTool({
    name: "validate_campaign",
    description:
      "Validate Yandex Direct campaign payload through local backend before creation.",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {
        campaign: {
          type: "object",
          additionalProperties: true,
        },
      },
      required: ["campaign"],
    },
    async execute(_toolCallId: string, params: { campaign: JsonRecord }) {
      const result = await postJson(baseUrl, "/validate_campaign", params.campaign);

      return {
        content: [
          {
            type: "text",
            text: "Validation finished.\n\n" + JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  });

  api.registerTool({
    name: "create_campaign",
    description:
      "Create Yandex Direct campaign through local backend only after explicit confirmation.",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {
        campaign: {
          type: "object",
          additionalProperties: true,
        },
        confirm: {
          type: "boolean",
        },
      },
      required: ["campaign", "confirm"],
    },
    async execute(
      _toolCallId: string,
      params: { campaign: JsonRecord; confirm: boolean }
    ) {
      if (!params.confirm) {
        return {
          content: [
            {
              type: "text",
              text: "Creation blocked: confirm=true is required.",
            },
          ],
        };
      }

      const result = await postJson(baseUrl, "/create_campaign", { ...params.campaign, confirm: true });

      return {
        content: [
          {
            type: "text",
            text: "Creation finished.\n\n" + JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  });

  api.logger.info(
    `yandex-direct-bridge registered tools: validate_campaign, create_campaign; baseUrl=${baseUrl}`
  );
}
