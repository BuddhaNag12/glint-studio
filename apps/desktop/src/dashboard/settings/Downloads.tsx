import { useEffect, useState } from "react";
import { api } from "../api";
import { Alert, SettingRow, SettingsSection } from "../ui";

// Mirrors net.DownloadSettings in host/app/net.py.
type Source = "huggingface" | "modelscope";
type Dl = {
  source: Source;
  hf_endpoint: string | null;
  http_proxy: string | null;
  https_proxy: string | null;
  no_proxy: string | null;
  ca_bundle: string | null;
};
type Field = Exclude<keyof Dl, "source">;

const DEFAULTS: Dl = { source: "huggingface", hf_endpoint: null, http_proxy: null, https_proxy: null, no_proxy: null, ca_bundle: null };
const SOURCES: [Source, string][] = [
  ["huggingface", "Hugging Face"],
  ["modelscope", "ModelScope"],
];
const FIELDS: [Field, string, string, string][] = [
  ["hf_endpoint", "Hugging Face mirror", "Used in place of huggingface.co.", "https://hf-mirror.com"],
  ["http_proxy", "HTTP proxy", "For http:// downloads.", "http://proxy:8080"],
  ["https_proxy", "HTTPS proxy", "For https:// downloads, which is nearly all of them.", "http://proxy:8080"],
  ["no_proxy", "Skip the proxy for", "Host names, separated by commas.", "localhost, .example.com"],
  ["ca_bundle", "Extra CA certificate", "Full path to a PEM file, for a proxy that checks HTTPS traffic.", "/path/to/ca.pem"],
];

/** Settings › Downloads: where model files come from, and the proxy they come through. Text fields save when you leave them. */
export default function Downloads() {
  const [saved, setSaved] = useState<Dl | null>(null);
  const [draft, setDraft] = useState<Dl>(DEFAULTS);
  const [error, setError] = useState<string | null>(null);
  const [catalog, setCatalog] = useState<{ available: boolean; enabled: boolean } | null>(null);

  useEffect(() => {
    api<{ available: boolean; enabled: boolean }>("/api/v1/catalog").then(setCatalog, () => setCatalog(null));
  }, []);
  const setChecking = (enabled: boolean) =>
    api<{ available: boolean; enabled: boolean }>("/api/v1/catalog", { method: "PUT", body: JSON.stringify({ enabled }) }).then(
      setCatalog,
      (e) => setError(e.message),
    );

  const apply = ({ error, ...s }: Dl & { error: string | null }) => (setSaved(s), setDraft(s), setError(error));
  useEffect(() => {
    api<Dl & { error: string | null }>("/api/v1/settings/downloads").then(apply, (e) => setError(e.message));
  }, []);
  const save = (next: Dl) =>
    api<Dl & { error: string | null }>("/api/v1/settings/downloads", { method: "PUT", body: JSON.stringify(next) }).then(apply, (e) =>
      setError(e.message),
    );

  const changed = saved !== null && JSON.stringify(saved) !== JSON.stringify(DEFAULTS);
  return (
    <SettingsSection id="downloads" title="Downloads" badge="live" onReset={changed ? () => save(DEFAULTS) : undefined}>
      {error && <Alert>{error}</Alert>}
      {catalog?.available && (
        <SettingRow
          label="Check for new models"
          id="catalog-check"
          hint="At each start, fetch the short list of models added since this version, from GitHub. Models download only when you pick one."
        >
          <input
            id="catalog-check"
            type="checkbox"
            role="switch"
            className="switch"
            checked={catalog.enabled}
            onChange={(e) => setChecking(e.target.checked)}
          />
        </SettingRow>
      )}
      {saved && (
        <>
          <SettingRow
            label="Download models from"
            hint="ModelScope is often faster in mainland China. Every file is checked against its known checksum, so a wrong source fails instead of installing a bad file."
          >
            <div className="chip-row">
              {SOURCES.map(([id, label]) => (
                <label key={id} className="chip">
                  <input type="radio" name="dl-source" value={id} checked={saved.source === id} onChange={() => save({ ...saved, source: id })} />
                  {label}
                </label>
              ))}
            </div>
          </SettingRow>
          {FIELDS.map(([key, label, hint, placeholder]) => (
            <SettingRow key={key} id={`dl-${key}`} label={label} hint={hint}>
              <input
                id={`dl-${key}`}
                type="text"
                spellCheck={false}
                autoComplete="off"
                placeholder={placeholder}
                value={draft[key] ?? ""}
                onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}
                onBlur={() => (draft[key]?.trim() || null) !== saved[key] && save({ ...saved, [key]: draft[key]?.trim() || null })}
              />
            </SettingRow>
          ))}
        </>
      )}
    </SettingsSection>
  );
}
