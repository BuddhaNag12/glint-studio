import { useEffect, useState } from "react";
import { api } from "../api";
import { Alert, SettingRow, SettingsSection, useConfirm } from "../ui";

type UsageSettings = { record_usage: boolean; keep_days: number };

export default function Usage() {
  const [settings, setSettings] = useState<UsageSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cleared, setCleared] = useState(false);
  const { dialog, ask } = useConfirm();

  useEffect(() => {
    api<UsageSettings>("/api/v1/usage/settings").then(setSettings, (e) => setError(e.message));
  }, []);

  const save = (record_usage: boolean) =>
    api<UsageSettings>("/api/v1/usage/settings", { method: "PUT", body: JSON.stringify({ record_usage }) }).then(
      setSettings,
      (e) => setError(e.message),
    );

  const clear = async () => {
    const ok = await ask({
      title: "Clear usage history?",
      body: "This deletes every job counted on the Dashboard. It can't be undone. Daily key limits keep counting.",
      action: "Clear history",
    });
    if (!ok) return;
    setError(null);
    await api("/api/v1/stats", { method: "DELETE" }).then(() => setCleared(true), (e) => setError(e.message));
  };

  return (
    <SettingsSection
      id="usage"
      title="Usage"
      badge="live"
      onReset={settings && !settings.record_usage ? () => save(true) : undefined}
    >
      {error && <Alert>{error}</Alert>}
      <SettingRow
        label="Record usage"
        id="record-usage"
        hint={`Counts each image for the Dashboard and keeps it in Generate's history. Off keeps what's there and adds nothing. History older than ${settings?.keep_days ?? 400} days is removed.`}
      >
        <input
          id="record-usage"
          type="checkbox"
          role="switch"
          className="switch"
          checked={settings?.record_usage ?? true}
          disabled={!settings}
          onChange={(e) => save(e.target.checked)}
        />
      </SettingRow>
      <SettingRow label="Usage history" hint={cleared ? "Cleared." : "Delete everything the Dashboard has counted."}>
        <button type="button" className="danger" onClick={clear}>
          Clear history
        </button>
      </SettingRow>
      {dialog}
    </SettingsSection>
  );
}
