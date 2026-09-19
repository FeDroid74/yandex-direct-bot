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
  globalThis.fetch = async (url, options) => {
    calls++;
    assert.equal(url, "http://127.0.0.1:8092/decision");
    assert.equal(options.headers.Authorization, "Bearer decision-test");
    const body = JSON.parse(options.body);
    assert.equal(body.sender_id, "42");
    assert.equal(body.revision, 2);
    return { ok: true, json: async () => ({ text: "Details read", state: "pending" }) };
  };
  const ctx = { senderId: "42", channel: "telegram", isAuthorizedSender: true, args: "details abcdef123456 2" };
  assert.equal((await command.handler(ctx)).text, "Details read");
  assert.equal(calls, 1);
  for (const override of [{ senderId: "43" }, { channel: "discord" }, { isAuthorizedSender: false }, { args: "approve abcdef123456 2 injected" }]) {
    await command.handler({ ...ctx, ...override });
    assert.equal(calls, 1);
  }
  assert.equal(registered.length, 5);
  assert.ok(registered.every((tool) => !/approve|execute|apply/.test(tool.name)));
  console.log("Plugin tests passed: owner validation, exact callback, no approval tool exposed.");
} finally {
  rmSync(dir, { recursive: true });
}
