import { useEffect, useState } from "react";
import { api, gb } from "../api";
import { Alert, SettingRow, SettingsSection, useConfirm } from "../ui";

// Mirrors /api/v1/settings/erase in host/app/net_settings.py.
type Info = { folder: string; total_bytes: number; models_bytes: number; linked_folders: number };

const size = (bytes: number) => (bytes < 1e9 ? `${Math.round(bytes / 1e6)} MB` : gb(bytes));

/** Settings › Erase: delete everything the app keeps on this computer, then quit. The app does the deleting. */
export default function Erase() {
  const [info, setInfo] = useState<Info | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [erasing, setErasing] = useState(false);
  const { dialog, ask } = useConfirm();

  useEffect(() => {
    api<Info>("/api/v1/settings/erase").then(setInfo, (e) => setError(e.message));
  }, []);

  const erase = async () => {
    if (!info) return;
    const ok = await ask({
      title: "Erase all data and quit?",
      body:
        `This deletes everything Glint Studio keeps on this computer, ${size(info.total_bytes)} in all: ` +
        `your downloaded models (${size(info.models_bytes)}), the engine and its Python, every image you've made, ` +
        "your styles and workflows, settings, app keys and cloud provider keys, usage history and logs. " +
        (info.linked_folders ? "Model folders you linked from another ComfyUI are left alone. " : "") +
        "Glint Studio then quits. This can't be undone.",
      action: "Erase and quit",
    });
    if (!ok) return;
    setError(null);
    api("/api/v1/settings/erase", { method: "POST" }).then(() => setErasing(true), (e) => setError(e.message));
  };

  return (
    <SettingsSection id="erase" title="Erase">
      {dialog}
      {error && <Alert>{error}</Alert>}
      <SettingRow
        label="Erase all data and quit"
        hint={
          erasing
            ? "Erasing. Glint Studio quits when it's done; you can close this page."
            : info
              ? `Frees ${size(info.total_bytes)}, ${size(info.models_bytes)} of it downloaded models. Do this before you uninstall.`
              : "Measuring…"
        }
      >
        <button type="button" className="danger" onClick={erase} disabled={!info || erasing}>
          Erase and quit
        </button>
      </SettingRow>
    </SettingsSection>
  );
}
