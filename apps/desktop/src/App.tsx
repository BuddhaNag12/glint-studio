import { useState } from "react";
import Onboarding, { type Engine } from "./pages/Onboarding";
import Setup, { type HostInfo } from "./pages/Setup";
import Providers from "./pages/Providers";
import "./App.css";

export default function App() {
  const [engine, setEngine] = useState<Engine | null>(null);
  const [host, setHost] = useState<HostInfo | null>(null);
  if (!engine) return <Onboarding onChoose={setEngine} />;
  // The local engine's next stop is the dashboard (Setup opens it); the cloud one needs a key first.
  if (host) return <Providers host={host} />;
  return <Setup engine={engine} onBack={() => setEngine(null)} onReady={setHost} />;
}
