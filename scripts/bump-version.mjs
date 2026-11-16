#!/usr/bin/env node
// Sets the app version in every file that carries it: node scripts/bump-version.mjs 0.2.0
import { readFileSync, writeFileSync } from "node:fs";

const version = process.argv[2];
if (!/^\d+\.\d+\.\d+$/.test(version ?? "")) {
  console.error("Usage: node scripts/bump-version.mjs <major.minor.patch>");
  process.exit(1);
}

const edit = (path, change) => {
  const before = readFileSync(path, "utf8");
  const after = change(before);
  if (after === before && !before.includes(version)) throw new Error(`No version found in ${path}`);
  writeFileSync(path, after);
  console.log(`${path}: ${version}`);
};
const json = (path, change) =>
  edit(path, (text) => {
    const data = JSON.parse(text);
    change(data);
    return JSON.stringify(data, null, 2) + "\n";
  });
// The first `version = "…"` line, which is the package's own in Cargo.toml and pyproject.toml.
const firstVersionLine = (text) => text.replace(/^version = ".*"$/m, `version = "${version}"`);

json("apps/desktop/package.json", (d) => (d.version = version));
json("apps/desktop/package-lock.json", (d) => {
  d.version = version;
  d.packages[""].version = version;
});
json("apps/desktop/src-tauri/tauri.conf.json", (d) => (d.version = version));
edit("apps/desktop/src-tauri/Cargo.toml", firstVersionLine);
edit("apps/desktop/src-tauri/Cargo.lock", (t) =>
  t.replace(/(name = "glint-studio"\nversion = )".*"/, `$1"${version}"`),
);
edit("host/pyproject.toml", firstVersionLine);
