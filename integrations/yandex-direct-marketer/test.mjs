import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import register from "./index.ts";

const dir = mkdtempSync(join(tmpdir(), "marketer-plugin-test-"));
try {
  const path = join(dir, "config.json");
  writeFileSync(path, JSON.stringify({ owner_id: "42", api_key: "read-test", decision_key: "decision-test" }));
  let command;
  const registered = [];
  register({ pluginConfig: { configFile: path }, registerCommand: (c) => { command = c; }, registerTool: (t) => registered.push(t), logger: { info() {} } });
  let calls = 0;
  let lastBody;
  const navigation = [[{ text: "Next", callback_data: "/yd details abcdef123456 2 3" }]];
  globalThis.fetch = async (url, options) => {
    calls++;
    assert.equal(url, "http://127.0.0.1:8092/decision");
    assert.equal(options.headers.Authorization, "Bearer decision-test");
    const body = JSON.parse(options.body);
    lastBody = body;
    assert.equal(body.sender_id, "42");
    assert.equal(body.revision, 2);
    return { ok: true, json: async () => ({ text: "Details read", state: "pending", buttons: body.page ? navigation : [] }) };
  };
  const ctx = { senderId: "42", channel: "telegram", isAuthorizedSender: true, args: "details abcdef123456 2" };
  assert.deepEqual(await command.handler(ctx), { text: "Details read" });
  assert.equal(lastBody.page, undefined);
  assert.equal(calls, 1);
  const paged = await command.handler({ ...ctx, senderId: "telegram:42", args: "details abcdef123456 2 2" });
  assert.deepEqual(paged.channelData.telegram.buttons, navigation);
  assert.equal(lastBody.page, 2);
  assert.equal(calls, 2);
  for (const override of [{ senderId: "43" }, { channel: "discord" }, { isAuthorizedSender: false },
      { args: "approve abcdef123456 2 injected" }, { args: "approve abcdef123456 2 1" },
      { args: "details abcdef123456 2 0" }, { args: "details abcdef123456 2 1.5" },
      { args: "details abcdef123456 9007199254740992" }, { args: "details abcdef123456 2 9007199254740992" }]) {
    await command.handler({ ...ctx, ...override });
    assert.equal(calls, 2);
  }
  await command.handler({ ...ctx, args: "approve abcdef123456 2" });
  assert.equal(lastBody.decision, "approve");
  assert.equal(lastBody.page, undefined);
  assert.equal(calls, 3);
  assert.equal(registered.length, 13);
  assert.ok(registered.every((tool) => !/approve|execute|apply/.test(tool.name)));
  console.log("Plugin tests passed: owner validation, paged details, native Telegram buttons, no approval tool exposed.");
} finally {
  rmSync(dir, { recursive: true });
}
