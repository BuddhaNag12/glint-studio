import { useState } from "react";
import { Connection } from "./api";

function Copy({ text, label }: { text: string; label: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      aria-label={`Copy ${label}`}
      onClick={() =>
        navigator.clipboard.writeText(text).then(() => {
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        })
      }
    >
      {done ? "Copied" : "Copy"}
    </button>
  );
}

export default function Connect({ conn }: { conn: Connection }) {
  const [shown, setShown] = useState(false);
  const curl = `curl ${conn.url}/v1/images/generations \\
  -H "Authorization: Bearer ${conn.api_key}" \\
  -H "Content-Type: application/json" \\
  -d '{"prompt": "a red fox in the snow", "style": "cartoon"}'`;

  return (
    <>
      <h1>Connect</h1>
      <p className="lead">
        Other apps on this computer reach Glint Studio here. It speaks the OpenAI images API, so most clients only need the
        address and the key.
      </p>
      <section className="card">
        <dl className="facts">
          <dt>Address</dt>
          <dd className="row">
            <code>{conn.url}</code>
            <Copy text={conn.url} label="address" />
          </dd>
          <dt>API key</dt>
          <dd className="row">
            <code>{shown ? conn.api_key : "•".repeat(24)}</code>
            <button onClick={() => setShown(!shown)}>{shown ? "Hide" : "Show"}</button>
            <Copy text={conn.api_key} label="API key" />
          </dd>
        </dl>
        <p className="lead note">The key changes each time Glint Studio starts.</p>
      </section>
      <section className="card">
        <div className="model-head">
          <h2>Try it</h2>
          <Copy text={curl} label="curl command" />
        </div>
        <pre className="snippet">{shown ? curl : curl.replace(conn.api_key, "•".repeat(24))}</pre>
      </section>
    </>
  );
}
