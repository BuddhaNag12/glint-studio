import { useEffect, useState } from "react";
import { api, ApiKey } from "./api";
import { Alert, CopyButton, Icon, Tabs, useConfirm } from "./ui";

type App = "claude-code" | "claude-desktop" | "open-webui" | "librechat" | "mcp";
const APPS: [App, string][] = [
  ["claude-code", "Claude Code"],
  ["claude-desktop", "Claude Desktop"],
  ["open-webui", "Open WebUI"],
  ["librechat", "LibreChat"],
  ["mcp", "Cursor and others"],
];
const NAME = Object.fromEntries(APPS) as Record<App, string>;

// Mirrors _desktop_view in host/app/mcp.py.
type Desktop = { path: string; installed: boolean; applied: boolean; backup: boolean };

const snippets = (url: string, key: string): Record<Exclude<App, "claude-desktop">, string> => ({
  "claude-code": `claude mcp add --transport http glint-studio ${url}/mcp --header "Authorization: Bearer ${key}"`,
  "open-webui": `Admin Panel → Settings → Images
Image Generation Engine: Default (Open AI)
API Base URL: ${url}/v1
API Key: ${key}
Model: z-image-turbo`,
  librechat: `# librechat.yaml
mcpServers:
  glint-studio:
    type: streamable-http
    url: ${url}/mcp
    headers:
      Authorization: "Bearer ${key}"`,
  mcp: JSON.stringify({ mcpServers: { "glint-studio": { url: `${url}/mcp`, headers: { Authorization: `Bearer ${key}` } } } }, null, 2),
});

const HELP: Record<Exclude<App, "claude-desktop">, string> = {
  "claude-code": "Run this in a terminal. Then ask Claude Code for an image.",
  "open-webui": "Open WebUI makes images through the OpenAI API. If it runs in Docker, use host.docker.internal instead of 127.0.0.1.",
  librechat: "Add this to librechat.yaml and restart LibreChat. If it runs in Docker, use host.docker.internal instead of 127.0.0.1.",
  mcp: "For Cursor (~/.cursor/mcp.json) and other apps that take an MCP server by URL.",
};

/** One-click and copy-paste setup for apps that make images through Glint Studio. */
export default function Integrations({ url }: { url: string }) {
  const [app, setApp] = useState<App>("claude-code");
  const [made, setMade] = useState<{ app: App; key: string } | null>(null);
  const [desktop, setDesktop] = useState<Desktop | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const confirm = useConfirm();

  useEffect(() => {
    api<Desktop>("/api/v1/integrations/claude-desktop").then(setDesktop, () => {});
  }, []);

  // Like pairing a phone: each app gets its own local-only key, shown once.
  const makeKey = (a: App) =>
    api<ApiKey & { key: string }>("/api/v1/keys", {
      method: "POST",
      body: JSON.stringify({ name: NAME[a], scopes: ["local"], daily_cap: null }),
    }).then((k) => (setMade({ app: a, key: k.key }), k.key));

  const run = (work: Promise<unknown>) => {
    setError(null);
    setBusy(true);
    work.catch((e) => setError(e.message)).finally(() => setBusy(false));
  };
  const apply = () =>
    run(
      (made?.app === "claude-desktop" ? Promise.resolve(made.key) : makeKey("claude-desktop")).then((key) =>
        api<Desktop>("/api/v1/integrations/claude-desktop", { method: "POST", body: JSON.stringify({ key }) }).then(setDesktop),
      ),
    );
  const restore = () =>
    confirm
      .ask({
        title: "Restore Claude Desktop's settings?",
        body: "This puts back its config file from before the first Apply. Changes made to it since then are lost.",
        action: "Restore",
      })
      .then((ok) => ok && run(api<Desktop>("/api/v1/integrations/claude-desktop", { method: "DELETE" }).then(setDesktop)));

  const key = made?.app === app ? made.key : "<your key>";

  return (
    <section className="card" aria-labelledby="apps">
      {confirm.dialog}
      <div className="card-head">
        <div>
          <h2 id="apps">Apps</h2>
          <p className="muted small">Make images from Claude, Cursor and chat apps. Each app gets its own key, which you can revoke above.</p>
        </div>
      </div>
      <Tabs variant="underline" label="App" value={app} onChange={(a) => (setError(null), setApp(a))} options={APPS} />
      {app === "claude-desktop" ? (
        <>
          <p>
            Apply adds Glint Studio to Claude Desktop's settings, with a new key for it. Restart Claude Desktop afterwards.
          </p>
          <p className="muted small">
            Claude Desktop starts a small bridge that ships with Glint Studio, so nothing else, such as Node.js, is needed.
            The first Apply keeps a backup that Restore puts back.
          </p>
          {desktop && (
            <p className="cap" role="status">
              {!desktop.installed
                ? "Claude Desktop isn't installed on this computer."
                : desktop.applied
                  ? "Glint Studio is in Claude Desktop's settings."
                  : "Glint Studio isn't in Claude Desktop's settings yet."}{" "}
              <code>{desktop.path}</code>
            </p>
          )}
          <div className="actions">
            <button className="primary" onClick={apply} disabled={busy || !desktop?.installed}>
              {busy && <Icon name="spinner" size={16} />}
              Apply
            </button>
            <button onClick={restore} disabled={busy || !desktop?.backup}>
              Restore
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="muted small">{HELP[app]}</p>
          <div className="card-head">
            <button className="small" onClick={() => run(makeKey(app))} disabled={busy}>
              <Icon name="key" size={16} />
              Create a key for {NAME[app]}
            </button>
            <CopyButton text={snippets(url, key)[app]} label={`${NAME[app]} setup`} />
          </div>
          {made?.app === app && <p className="cap" role="status">Copy this now. The key won't be shown again.</p>}
          <pre className="snippet">{snippets(url, key)[app]}</pre>
        </>
      )}
      {error && <Alert>{error}</Alert>}
    </section>
  );
}
