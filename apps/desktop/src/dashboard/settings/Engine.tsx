import { useEffect, useState } from "react";
import { api } from "../api";
import { EngineServer, MemoryGuard, restartEngine } from "../ServerCard";
import { Alert, SettingRow, SettingsSection } from "../ui";

const IDLE: [number, string][] = [
  [0, "Never"],
  [15, "15 min"],
  [30, "30 min"],
  [60, "1 h"],
  [120, "2 h"],
  [480, "8 h"],
  [1440, "24 h"],
];
const GUARDS: [MemoryGuard, string, string][] = [
  ["safe", "Safe", "Uses less memory and keeps 2 GB free for other apps. Slower."],
  ["balanced", "Balanced", "ComfyUI's own defaults. Right for most computers."],
  ["aggressive", "Aggressive", "Keeps models in GPU memory between images. Fastest, needs the most memory."],
  ["custom", "Custom", "Keep a set amount of memory free for other apps."],
];

type Settings = EngineServer["settings"];

/** Settings › Engine: loading at start, idle unload, and the memory guard ComfyUI starts with. */
export default function Engine() {
  const [preload, setPreload] = useState<boolean | null>(null);
  const [server, setServer] = useState<EngineServer | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<{ preload: boolean }>("/api/v1/engine/settings").then((s) => setPreload(s.preload), (e) => setError(e.message));
    api<EngineServer>("/api/v1/engine/server").then(setServer, (e) => setError(e.message));
  }, []);

  const run = <T,>(call: () => Promise<T>, done: (v: T) => void) => {
    setBusy(true);
    call()
      .then(done)
      .then(() => setError(null), (e) => setError(e.message))
      .finally(() => setBusy(false));
  };
  const savePreload = (on: boolean) =>
    run(() => api<{ preload: boolean }>("/api/v1/engine/settings", { method: "PUT", body: JSON.stringify({ preload: on }) }), (s) => setPreload(s.preload));
  const save = (patch: Partial<Settings>) =>
    run(() => api<EngineServer>("/api/v1/engine/server", { method: "PUT", body: JSON.stringify(patch) }), setServer);

  const s = server?.settings;
  const idleOptions = s && !IDLE.some(([m]) => m === s.idle_unload_minutes) ? [...IDLE, [s.idle_unload_minutes, `${s.idle_unload_minutes} min`] as [number, string]] : IDLE;

  return (
    <>
      <SettingsSection
        id="engine"
        title="Engine"
        badge="live"
        onReset={preload || s?.idle_unload_minutes ? () => (savePreload(false), save({ idle_unload_minutes: 0 })) : undefined}
      >
        {error && <Alert>{error}</Alert>}
        {preload !== null && (
          <SettingRow id="engine-preload" label="Load a model when Glint Studio starts" hint="The first image skips the wait. Uses the default model, or the first one installed.">
            <input id="engine-preload" type="checkbox" className="switch" role="switch" checked={preload} disabled={busy} onChange={(e) => savePreload(e.target.checked)} />
          </SettingRow>
        )}
        {s && (
          <SettingRow id="engine-idle" label="Unload the model when idle" hint="Frees memory after this long with no images.">
            <select id="engine-idle" value={s.idle_unload_minutes} disabled={busy} onChange={(e) => save({ idle_unload_minutes: Number(e.target.value) })}>
              {idleOptions.map(([m, label]) => (
                <option key={m} value={m}>
                  {label}
                </option>
              ))}
            </select>
          </SettingRow>
        )}
      </SettingsSection>

      {s && (
        <SettingsSection id="engine-memory" title="Memory guard" badge="restart" onReset={s.memory_guard !== "balanced" ? () => save({ memory_guard: "balanced" }) : undefined}>
          <SettingRow label="Preset" hint={GUARDS.find(([g]) => g === s.memory_guard)?.[2]}>
            <div className="chip-row">
              {GUARDS.map(([g, label]) => (
                <label key={g} className="chip">
                  <input type="radio" name="memory-guard" value={g} checked={s.memory_guard === g} disabled={busy} onChange={() => save({ memory_guard: g })} />
                  {label}
                </label>
              ))}
            </div>
          </SettingRow>
          {s.memory_guard === "custom" && (
            <SettingRow id="engine-reserve" label="Memory to keep free" hint="In GB, for your system and other apps.">
              <input
                id="engine-reserve"
                key={s.reserve_vram_gb}
                type="number"
                min={0}
                max={128}
                step={0.5}
                style={{ width: 96 }}
                defaultValue={s.reserve_vram_gb}
                disabled={busy}
                onBlur={(e) => {
                  const gb = Number(e.target.value);
                  if (e.target.value !== "" && gb >= 0 && gb <= 128 && gb !== s.reserve_vram_gb) save({ reserve_vram_gb: gb });
                }}
              />
            </SettingRow>
          )}
          {server?.restart_needed && (
            <SettingRow label="Restart needed" hint="The engine is still running with the old setting. Restarting unloads the model.">
              <button className="small" disabled={busy} onClick={() => run(restartEngine, setServer)}>
                Restart
              </button>
            </SettingRow>
          )}
        </SettingsSection>
      )}
    </>
  );
}
