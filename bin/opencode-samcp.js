#!/usr/bin/env node
// npm launcher for opencode-samcp. The server itself is Python; this shim runs the
// copy bundled in this package through uv (uvx), so no manual Python setup is needed.
const { spawn, spawnSync } = require("node:child_process");
const path = require("node:path");

const pkgRoot = path.resolve(__dirname, "..");
const args = process.argv.slice(2);

function has(cmd) {
  return spawnSync(cmd, ["--version"], { stdio: "ignore" }).status === 0;
}

let cmd, cmdArgs;
if (has("uvx")) {
  [cmd, cmdArgs] = ["uvx", ["--from", pkgRoot, "opencode-samcp", ...args]];
} else if (has("uv")) {
  [cmd, cmdArgs] = ["uv", ["tool", "run", "--from", pkgRoot, "opencode-samcp", ...args]];
} else {
  console.error(
    "opencode-samcp failed: uv is not installed on machine (needed to run the Python server). " +
      "Install it from https://docs.astral.sh/uv/getting-started/installation/"
  );
  process.exit(1);
}

const child = spawn(cmd, cmdArgs, { stdio: "inherit" });
for (const sig of ["SIGINT", "SIGTERM"]) process.on(sig, () => child.kill(sig));
child.on("error", (e) => { console.error(`opencode-samcp failed to start: ${e.message}`); process.exit(1); });
child.on("exit", (code, signal) => process.exit(signal ? 1 : code ?? 1));
