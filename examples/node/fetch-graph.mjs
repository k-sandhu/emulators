// Node 22+: trust the local emulator CA with NODE_EXTRA_CA_CERTS before starting Node.
const gateway = process.env.EMULATORS_GATEWAY ?? "https://localhost:44443";
const base = `${gateway.replace(/\/$/, "")}/microsoft`;

async function jsonResponse(response) {
  if (!response.ok) {
    throw new Error(`Emulator returned HTTP ${response.status}: ${await response.text()}`);
  }
  return response.json();
}

async function main() {
  const grant = await jsonResponse(await fetch(`${base}/_emulator/tokens`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user: "p018" }),
  }));
  const me = await jsonResponse(await fetch(`${base}/v1.0/me`, {
    headers: { Authorization: `Bearer ${grant.access_token}` },
  }));
  if (me.mail !== "maya.chen@halcyonadvisory.example") {
    throw new Error("Unexpected synthetic employee");
  }
  process.stdout.write(`${JSON.stringify({ name: me.displayName, email: me.mail }, null, 2)}\n`);
}

await main();
