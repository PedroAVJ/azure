import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import test from "node:test";

test("Azure Git helper scopes credentials and negotiates Bearer without exposing provider output", () => {
  const root = fileURLToPath(new URL("..", import.meta.url));
  const result = spawnSync("python3", ["-B", "tests/test_git_credential.py"], {
    cwd: root,
    encoding: "utf8",
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
  assert.equal(result.status, 0, result.stderr || result.stdout || result.error?.message);
});
