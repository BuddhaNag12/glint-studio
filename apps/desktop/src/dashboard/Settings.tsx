import { useEffect, useState } from "react";
import type { System } from "./api";
import { PageHeader } from "./ui";
import Engine from "./settings/Engine";
import Downloads from "./settings/Downloads";
import Security from "./settings/Security";
import Usage from "./settings/Usage";
import Appearance from "./settings/Appearance";
import Erase from "./settings/Erase";

// Each section component renders a SettingsSection with the matching id; main.tsx scrolls #settings/<id> into view.
const SECTIONS = [
  ["engine", "Engine"],
  ["downloads", "Downloads"],
  ["security", "Security"],
  ["usage", "Usage"],
  ["appearance", "Appearance"],
  ["erase", "Erase"],
] as const;

export default function Settings({ sub, system }: { sub: string; system: System | null }) {
  const [current, setCurrent] = useState(sub || SECTIONS[0][0]);
  useEffect(() => {
    if (sub) setCurrent(sub);
  }, [sub]);

  // Highlight the last section whose top has passed under the top bar. At the bottom of the page the last sections
  // can't reach the top, so there it's the linked one if it shows, else the last one.
  // Measured on scroll rather than observed, since some sections render only after their first fetch.
  useEffect(() => {
    const onScroll = () => {
      const bottom = innerHeight + scrollY >= document.documentElement.scrollHeight - 2;
      let at: string = SECTIONS[0][0];
      for (const [id] of SECTIONS) {
        const el = document.getElementById(id);
        if (el && el.getBoundingClientRect().top <= 150) at = id;
      }
      if (bottom) {
        const linked = location.hash.split("/")[1];
        const top = linked ? document.getElementById(linked)?.getBoundingClientRect().top : undefined;
        at = top !== undefined && top < innerHeight ? linked : SECTIONS[SECTIONS.length - 1][0];
      }
      setCurrent(at);
    };
    addEventListener("scroll", onScroll, { passive: true });
    return () => removeEventListener("scroll", onScroll);
  }, []);

  return (
    <>
      <PageHeader title="Settings" />
      <div className="settings">
        <nav className="settings-nav" aria-label="Settings sections">
          <ul>
            {SECTIONS.map(([id, label]) => (
              <li key={id}>
                <a href={`#settings/${id}`} aria-current={id === current ? "true" : undefined}>
                  {label}
                </a>
              </li>
            ))}
          </ul>
        </nav>
        <div className="settings-body">
          <Engine system={system} />
          <Downloads />
          <Security />
          <Usage />
          <Appearance />
          <Erase />
        </div>
      </div>
    </>
  );
}
