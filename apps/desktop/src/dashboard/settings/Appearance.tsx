import { useState } from "react";
import { SettingRow, SettingsSection } from "../ui";

// Per viewer, in this browser only. Storage can throw (private mode, blocked site data), so every access is guarded.
type Theme = "auto" | "light" | "dark";
const THEME = "glint.theme";
const READABLE = "glint.readable";
const THEMES: [Theme, string][] = [
  ["auto", "Auto"],
  ["light", "Light"],
  ["dark", "Dark"],
];

const read = (key: string) => {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
};
const write = (key: string, value: string | null) => {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* the choice still applies until the page reloads */
  }
};
const storedTheme = (): Theme => {
  const t = read(THEME);
  return t === "light" || t === "dark" ? t : "auto";
};

/** Puts the stored choices on <html>. main.tsx calls it before the first render so the page never flashes. */
export function applyAppearance(theme = storedTheme(), readable = read(READABLE) === "1") {
  const html = document.documentElement;
  if (theme === "auto") html.removeAttribute("data-theme");
  else html.dataset.theme = theme;
  html.classList.toggle("readable", readable);
}

export default function Appearance() {
  const [theme, setTheme] = useState(storedTheme);
  const [readable, setReadable] = useState(() => read(READABLE) === "1");
  const update = (t: Theme, r: boolean) => {
    setTheme(t);
    setReadable(r);
    write(THEME, t === "auto" ? null : t);
    write(READABLE, r ? "1" : null);
    applyAppearance(t, r);
  };

  return (
    <SettingsSection id="appearance" title="Appearance" badge="live" onReset={theme !== "auto" || readable ? () => update("auto", false) : undefined}>
      <SettingRow label="Theme" hint="Auto follows your system.">
        <div className="chip-row">
          {THEMES.map(([id, label]) => (
            <label key={id} className="chip">
              <input type="radio" name="theme" value={id} checked={theme === id} onChange={() => update(id, readable)} />
              {label}
            </label>
          ))}
        </div>
      </SettingRow>
      <SettingRow id="readable" label="Enhanced readability" hint="Stronger secondary text, and no text under 12px.">
        <input id="readable" type="checkbox" className="switch" role="switch" checked={readable} onChange={(e) => update(theme, e.target.checked)} />
      </SettingRow>
    </SettingsSection>
  );
}
