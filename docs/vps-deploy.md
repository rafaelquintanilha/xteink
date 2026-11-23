# Running the server on a VPS behind Caddy

This is how the server runs when the reader should sync from anywhere and no
machine at home is involved. It is an alternative to the Cloudflare Tunnel setup
in [`remote-access.md`](remote-access.md): the host already has a Caddy that
terminates TLS with Let's Encrypt, and Caddy fronts the api container.

Placeholders: `<vps>` is the SSH target and `<host>` the public hostname.

## What runs where

| Piece | Where | Reachable from |
|-------|-------|----------------|
| `api` container (main app 8780, device app 8781) | `<vps>:/opt/apps/apps/xteink`, compose project `xteink` | The host's loopback, and Caddy over the shared docker network as `xteink-api` |
| Library data | Docker volume `xteink_xteink-data` | The container only. It is not backed up |
| `mcp` container | Not started | Run the MCP server where the agent is, pointed at `https://<host>` |

Two files on the host are not in this repository: `.env` (binds the published
ports to `127.0.0.1`) and `compose.override.yaml` (joins the api to Caddy's
network with the alias `xteink-api`).

## Public routes

One hostname, one Caddy site. Everything that is not listed is a 404, so the
web UI and `/docs` are not exposed.

```caddy
<host> {
  header X-Robots-Tag "noindex, nofollow"

  # The reader verifies this certificate against pinned roots, which include
  # Let's Encrypt's. Keep Caddy from falling back to another issuer.
  tls {
    issuer acme
    # RSA: the reader verifies an RSA chain several times faster than ECDSA P-384.
    key_type rsa2048
  }

  # The reader: device protocol only, every route needs a device key.
  @device path /api/device/*
  handle @device {
    reverse_proxy xteink-api:8781
  }

  # Phone shortcut, agents and the CLI: every route needs an API key.
  @api path /api/*
  handle @api {
    reverse_proxy xteink-api:8780
  }

  handle {
    respond 404
  }
}
```

`/api/device/*` does not match `/api/devices`, so the device list stays on the
main app behind an API key. Validate before reloading, and reload instead of
restarting so the other sites keep their connections:

```bash
docker exec apps-caddy-1 caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker exec apps-caddy-1 caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

`<host>` needs a DNS A record to the VPS first (DNS only, not proxied); Caddy
gets the certificate when the site is loaded.

The Caddyfile is bind-mounted as a single file, so the container can hold an
older copy than the host: a tool that replaces the file leaves the container
on the old one, and `caddy reload` then reports `config is unchanged`. Compare
the two before trusting a reload, and write the host file into the container
when they differ:

```bash
docker exec apps-caddy-1 md5sum /etc/caddy/Caddyfile; md5sum /opt/apps/caddy/Caddyfile
docker exec -i apps-caddy-1 sh -c 'cat > /etc/caddy/Caddyfile' < /opt/apps/caddy/Caddyfile
```

## The certificate and the reader

The firmware trusts five pinned roots (`docs/xteink/tls.md` in the firmware
repository), among them ISRG Root X1 and X2. Caddy's Let's Encrypt
certificates chain to those, and Caddy sends the cross-signed intermediate the
reader needs. The `tls` block above keeps Caddy on Let's Encrypt: with
another issuer the reader would stop syncing.

`key_type rsa2048` makes the chain RSA end to end (leaf ← YR1/YR2 ← Root YR
← ISRG Root X1). Caddy's default ECDSA chain (leaf ← YE1 ← Root YE ← ISRG
Root X2, cross-signed by X1) needs three P-384 signature checks, which the
ESP32-C3 does in software; RSA checks with a small public exponent are cheap.
Measured with the firmware's wolfSSL build on a Mac
(`scripts/xteink-tls/host_check.cpp` in the firmware repository): 11.5 ms of
CPU per verified handshake with the ECDSA chain, 2.9 ms with the RSA chain,
and a handshake heap peak of 39.7 KB and 41.1 KB. The reader opens three
connections per delivery, so the saving repeats three times.

Caddy keeps a loaded certificate in memory across reloads, so changing
`key_type` does not replace a certificate that is still valid. To get the new
key type now, move the stored certificate out of
`/data/caddy/certificates/acme-v02.api.letsencrypt.org-directory/<host>/`,
reload once with the site removed, then reload with it back. Only `<host>`
is down in between; a container restart would interrupt every site on the
host.

## Keys

API keys and device keys are printed once and never stored in plain text.

```bash
# On the host, in /opt/apps/apps/xteink: one key per client.
docker compose exec api python -m xteink.server create-key <name>
```

The reader gets its device key over USB. With no LAN server, give it an empty
LAN address, so that it does not spend 4 seconds looking for one on every
sync:

```bash
XTEINK_URL=https://<host> XTEINK_API_KEY=<api-key> \
  uv run xteink device provision --lan-url "" --tunnel-url https://<host> --apply
```

## Firmware updates

The reader only ever talks to `https://<host>`, which the firmware counts as
the tunnel origin. An offered image is installed only by a build with
`-DXTEINK_OTA_TUNNEL=1` (set in the fork's `gh_release_rc` env). Upload and
offer with `xteink firmware upload` and `xteink firmware offer`
([`firmware-release.md`](firmware-release.md)); the reader installs it on a
daily run and rolls back by itself if the new image cannot reach the server.

## Updating the server

From this checkout, copy the tracked files over and rebuild. The host's `.env`
and `compose.override.yaml` are not in the archive, so they are kept.

```bash
git ls-files -co --exclude-standard -z | tar --null -T - -czf - \
  | ssh <vps> 'cd /opt/apps/apps/xteink && tar -xzf - && docker compose up -d --build api'
```
