import { useEffect, useState } from "react";
import { api } from "../api";
import { Alert, SettingRow, SettingsSection, useConfirm } from "../ui";

// Mirrors /api/v1/settings/security in host/app/net_settings.py.
type Sec = { lan_open_generation: boolean; lan_active: boolean };

/** Settings › Security: keyless image making on the local network, and what app keys can do. */
export default function Security() {
  const [s, setS] = useState<Sec | null>(null);
  const [error, setError] = useState<string | null>(null);
  const confirm = useConfirm();

  useEffect(() => {
    api<Sec>("/api/v1/settings/security").then(setS, (e) => setError(e.message));
  }, []);
  const save = (on: boolean) =>
    api<Sec>("/api/v1/settings/security", { method: "PUT", body: JSON.stringify({ lan_open_generation: on }) }).then(
      (r) => (setS(r), setError(null)),
      (e) => setError(e.message),
    );
  const toggle = async (on: boolean) => {
    const ok =
      !on ||
      (await confirm.ask({
        title: "Allow images without a key?",
        body: "Anyone on your network, guests included, could make images on this computer without asking. Only turn this on at home or on a network you trust.",
        action: "Allow",
      }));
    if (ok) save(on);
  };

  return (
    <SettingsSection id="security" title="Security" badge="live" onReset={s?.lan_open_generation ? () => save(false) : undefined}>
      {confirm.dialog}
      {error && <Alert>{error}</Alert>}
      {s && (
        <>
          <SettingRow
            id="lan-open"
            label="Allow image generation without a key from your local network"
            hint="Only for making images with models on this computer, from devices on your network. Never through the public link, and never for the dashboard, keys or settings."
          >
            <input id="lan-open" type="checkbox" className="switch" role="switch" checked={s.lan_open_generation} onChange={(e) => toggle(e.target.checked)} />
          </SettingRow>
          {s.lan_open_generation && (
            <Alert>
              Anyone on your network can make images here without a key.
              {!s.lan_active && ' It starts working when "On your local network" is on, on the Connect page.'}
            </Alert>
          )}
          <p className="help">
            Keys for apps and phones, made on the Connect page, only make images. They can't open this dashboard or change
            settings; the dashboard opens only from the Glint Studio menu on this computer.
          </p>
        </>
      )}
    </SettingsSection>
  );
}
