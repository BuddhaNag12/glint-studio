import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";

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

  return (
    <main className="page">
      <h1>Welcome to Glint Studio</h1>
      <p className="lead">First, a quick look at this computer to see what it can run.</p>

      <section className="card" aria-live="polite" aria-busy={!profile && !error}>
        {error ? (
          <>
            <p className="error">The scan didn't work: {error}</p>
            <button onClick={scan}>Try again</button>
          </>
        ) : !profile ? (
          <p>Scanning your hardware…</p>
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
              <dd>{profile.ram_gb} GB</dd>
              <dt>Free disk</dt>
              <dd>{profile.disk_free_gb} GB</dd>
              <dt>Graphics</dt>
              <dd>
                {profile.gpus.length === 0
                  ? "None found"
                  : profile.gpus
                      .map((g) => (g.vram_gb ? `${g.name}, ${g.vram_gb} GB` : g.name))
                      .join("; ")}
              </dd>
              <dt>Runs models on</dt>
              <dd>
                {BACKEND_LABEL[profile.backend]}
                {profile.vram_budget_gb > 0 && `, up to ${profile.vram_budget_gb} GB for models`}
              </dd>
            </dl>
            {profile.warnings.length > 0 && (
              <ul className="warnings">
                {profile.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            )}
          </>
        )}
      </section>

      {profile && (
        <section className="choices">
          <h2>How do you want to make images?</h2>
          <button className={gpuReady ? "choice primary" : "choice"} onClick={() => onChoose("local")}>
            <strong>On this computer</strong>
            <span>
              Free and private. Installs the image engine (a few GB) and a model that fits.
              {!gpuReady && " Without a supported GPU this is slow."}
            </span>
          </button>
          <button className={gpuReady ? "choice" : "choice primary"} onClick={() => onChoose("cloud")}>
            <strong>With a cloud provider</strong>
            <span>Fast on any computer. Uses your own API key, billed by the provider. Small install.</span>
          </button>
        </section>
      )}
    </main>
  );
}
