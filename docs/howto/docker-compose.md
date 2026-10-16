# Run the emulators next to your stack with real hostnames

Our services hard-code `https://graph.microsoft.com`, `https://api.zoom.us` and friends and refuse
non-HTTPS URLs. In Docker you can keep those URLs: the emulator container joins a shared network
under every vendor hostname (network aliases) and serves HTTPS with a certificate from a local CA.

```bash
docker compose -f docker-compose.yml up -d --build      # in this repo
```

This starts `emulators` on the `emulators-vendors` network with aliases for every vendor host,
listening on 443 (HTTPS) and 80 (HTTP) inside the network, and publishes the gateway on
`localhost:44080` / `localhost:44443` plus one port per provider (44101–44111). The CA lives in the
`emulators-certs` volume.

In your project's compose file:

```yaml
services:
  api:
    environment:
      SSL_CERT_FILE: /emulator-certs/ca-bundle.pem        # Python ssl / httpx
      REQUESTS_CA_BUNDLE: /emulator-certs/ca-bundle.pem   # requests
      NODE_EXTRA_CA_CERTS: /emulator-certs/ca.pem         # Node
    volumes:
      - emulators-certs:/emulator-certs:ro
    networks: [default, emulators-vendors]

volumes:
  emulators-certs:
    external: true
networks:
  emulators-vendors:
    external: true
```

Now `https://graph.microsoft.com/v1.0/me` from the `api` container reaches the emulator.

## SSRF guards

Our apps block private IP ranges for outbound calls (parlance's `PublicTransport`, lumen-copilot's
egress guard, saas_backbone's `SSRF_PROTECTION`). Docker networks use private addresses, so enable
the app's development/test escape hatch (e.g. `BACKBONE_CONNECTORS__SSRF_PROTECTION=false`) when
pointing it at the emulators. Never ship that setting to production.

## Without Docker

```bash
uv run emulators certs                 # writes .emulators/certs/{ca.pem,ca-bundle.pem,...}
uv run emulators hosts >> /etc/hosts   # 127.0.0.1 graph.microsoft.com ... (needs admin)
sudo uv run emulators serve --port 80 --https-port 443
```

Or skip hostnames entirely and use the path-prefix gateway (`http://localhost:44080/microsoft/v1.0/me`)
or the per-provider ports when your client lets you configure a base URL.
