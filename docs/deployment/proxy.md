# The proxy

`proxy` (Caddy) is the stack's only published port. `deploy/caddy/Caddyfile`
routes by path:

| | |
|---|---|
| `/` | the console |
| `/cal/` | Radicale — this is what a phone subscribes to |
| `/v1` | the read API. `/api/…` 308s onto it |

`docker compose up -d` publishes `8730` and nothing else. calsync's own writes
do not go through it: the poller reaches `http://radicale:5232` directly on the
compose network.

## Radicale under a path

`/cal` is not a plain reverse proxy, and getting it wrong looks like a
credentials problem.

A CalDAV client does not construct URLs. It asks for the principal, reads the
absolute `<href>` it gets back, requests that, and follows hrefs down. A server
that does not know it is mounted under a path answers with `/calsync/family/`,
the client requests that at the root, and gets the console's 404.
`X-Script-Name: /cal` is how Radicale is told, and it prefixes every href it
emits.

**The proxy must not strip the prefix.** Radicale strips it itself — any
`X-Forwarded-*` header puts it in reverse-proxy mode, and every proxy sets
those. Stripping in both places strips by string rather than by path segment:

```python
"/calsync/family/".removeprefix("/cal")   # -> "sync/family/"
```

which fails an assertion inside Radicale and answers 500 to every request. The
writer account is `calsync`, so `/cal` is a prefix of its principal path. Hence
`handle`, never `handle_path`.

`/.well-known/caldav` redirects to `/cal/` so that "add an account" works when
somebody enters the hostname alone.

## The API namespace

`/v1` is the mount; `/api/…` is a 308 onto it.

`GET /v1` serves a contract whose paths are server-absolute, generated from the
route table and asserted against it in both directions (`docs/API.md`). Mounted
under a stripped `/api`, every path it publishes would 404 on the console. At
`/v1` the paths in the document are correct on the origin that served it.

308 rather than 301 because the one write endpoint is a `POST`.

## One origin

The three services share a browser origin. The console's write guard is a
same-origin check and it has no login, so anything else served on this port is
same-origin with it. That holds for these three — Radicale serves stored items
as `text/calendar`, its web UI is static, and the API takes a bearer token and
sets no cookies. Anything serving attacker-influenced HTML needs its own port.

## Checks

```bash
curl -sI localhost:8730/                    # 200, the console
curl -sI localhost:8730/cal/                # 302 to /cal/.web
curl -s   localhost:8730/.well-known/caldav # 301 to /cal/
curl -s -H 'Authorization: Bearer <token>' localhost:8730/v1
```

Every href in this must begin `/cal/`:

```bash
curl -s -u calsync:<password> -X PROPFIND -H 'Depth: 0' \
  --data '<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop><D:current-user-principal/></D:prop></D:propfind>' \
  localhost:8730/cal/
```

The R1–R8 acceptance checks run through this path by default —
`CALSYNC_ACCEPTANCE_URL` defaults to `http://localhost:8730/cal`.

## Editing

`bootstrap` writes `config/caddy/Caddyfile` on the first `up` and never
overwrites it. `docker compose restart proxy` picks up a change;
`docker compose exec proxy caddy validate --config /etc/caddy/Caddyfile` checks
one first.

`proxy` has no `depends_on` on the services it fronts. Caddy resolves upstreams
per request, so a backend that is down is a 502 on its own path.

TLS is not configured: the port is plain HTTP and whatever fronts the host
terminates it. Serving HTTPS from Caddy directly needs a hostname and a volume
for `/data`, where it keeps certificates.
