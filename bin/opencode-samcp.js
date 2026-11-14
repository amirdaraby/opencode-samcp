#!/usr/bin/env node
// npm launcher for opencode-samcp. The server itself is Python; this shim runs the
// copy bundled in this package through uv (uvx), so no manual Python setup is needed.
const { spawn, spawnSync } = require("node:child_process");
const path = require("node:path");

const pkgRoot = path.resolve(__dirname, "..");
// postinstall: build uv's cached env now, so the first MCP start doesn't download deps
// and time out in clients like Claude Desktop. Never fail the install over it.
const warmup = process.env.npm_lifecycle_event === "postinstall";
const args = warmup ? ["--help"] : process.argv.slice(2);

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
  process.exit(warmup ? 0 : 1);
}

if (warmup) {
  const r = spawnSync(cmd, cmdArgs, { stdio: ["ignore", "ignore", "inherit"] });
  if (r.status !== 0) console.error("opencode-samcp: warm-up failed; the first start may be slow.");
  process.exit(0);
}

const child = spawn(cmd, cmdArgs, { stdio: "inherit" });
for (const sig of ["SIGINT", "SIGTERM"]) process.on(sig, () => child.kill(sig));
child.on("error", (e) => { console.error(`opencode-samcp failed to start: ${e.message}`); process.exit(1); });
child.on("exit", (code, signal) => process.exit(signal ? 1 : code ?? 1));
