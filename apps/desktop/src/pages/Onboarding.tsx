import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Alert, Icon } from "../dashboard/ui";

// Mirrors HardwareProfile in src-tauri/src/scan.rs.
type Backend = "cuda" | "mps" | "rocm" | "xpu" | "cpu";
type Gpu = { vendor: string; name: string; vram_gb: number | null; driver: string | null };
type HardwareProfile = {
  os: string;
  arch: string;
  cpu: string;
  ram_gb: number;
  disk_free_gb: number;
  gpus: Gpu[];
  backend: Backend;
  vram_budget_gb: number;
  warnings: string[];
};
export type Engine = "local" | "cloud";

const BACKEND_LABEL: Record<Backend, string> = {
  cuda: "NVIDIA GPU (CUDA)",
  mps: "Apple Silicon GPU (Metal)",
  rocm: "AMD GPU (ROCm)",
  xpu: "Intel Arc GPU",
  cpu: "CPU only",
};
const OS_LABEL: Record<string, string> = { macos: "macOS", windows: "Windows", linux: "Linux" };

export default function Onboarding({ onChoose }: { onChoose: (engine: Engine) => void }) {
  const [profile, setProfile] = useState<HardwareProfile | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function scan() {
    setProfile(null);
    setError(null);
    try {
      setProfile(await invoke<HardwareProfile>("scan_hardware"));
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => {
    scan();
  }, []);

  const gpuReady = profile !== null && profile.backend !== "cpu" && profile.vram_budget_gb > 0;

  const choice = (engine: Engine, recommended: boolean, title: string, body: string) => (
    <button className={recommended ? "choice recommended" : "choice"} onClick={() => onChoose(engine)}>
      <strong>
        {title}
        {recommended && <span className="tag">Recommended</span>}
      </strong>
      <span>{body}</span>
    </button>
  );

  return (
    <main className="page">
      <div className="page-intro">
        <span className="mark large" aria-hidden="true" />
        <h1>Welcome to Glint Studio</h1>
        <p className="lead">First, a quick look at this computer to see what it can run.</p>
      </div>

      <section className="card" aria-live="polite" aria-busy={!profile && !error} aria-labelledby="computer-title">
        <h2 id="computer-title">This computer</h2>
        {error ? (
          <>
            <Alert>The scan didn't work: {error}</Alert>
            <div className="actions">
              <button onClick={scan}>Try again</button>
            </div>
          </>
        ) : !profile ? (
          <p className="status quiet">
            <Icon name="spinner" size={16} />
            Scanning your hardware…
          </p>
        ) : (
          <>
            <dl className="facts">
              <dt>System</dt>
              <dd>
                {OS_LABEL[profile.os] ?? profile.os} ({profile.arch})
              </dd>
              <dt>Processor</dt>
              <dd>{profile.cpu || "Unknown"}</dd>
              <dt>Memory</dt>
              <dd className="num">{profile.ram_gb} GB</dd>
              <dt>Free disk</dt>
              <dd className="num">{profile.disk_free_gb} GB</dd>
              <dt>Graphics</dt>
              <dd>
                {profile.gpus.length === 0
                  ? "None found"
                  : profile.gpus.map((g) => (g.vram_gb ? `${g.name}, ${g.vram_gb} GB` : g.name)).join("; ")}
              </dd>
              <dt>Runs models on</dt>
              <dd>
                {BACKEND_LABEL[profile.backend]}
                {profile.vram_budget_gb > 0 && `, up to ${profile.vram_budget_gb} GB for models`}
              </dd>
            </dl>
            {profile.warnings.map((w) => (
              <Alert key={w} info>
                {w}
              </Alert>
            ))}
          </>
        )}
      </section>

      {profile && (
        <section className="choices" aria-labelledby="how-title">
          <h2 id="how-title">How do you want to make images?</h2>
          {choice(
            "local",
            gpuReady,
            "On this computer",
            `Free and private. Installs the image engine (a few GB) and a model that fits.${gpuReady ? "" : " Without a supported GPU this is slow."}`,
          )}
          {choice("cloud", !gpuReady, "With a cloud provider", "Fast on any computer. Uses your own API key, billed by the provider. Small install.")}
        </section>
      )}
    </main>
  );
}
