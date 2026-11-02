# Pushed

A source with no feed. Something, usually an AI agent, reads a team's schedule
from a PDF, an email or a photo, and sends calsync the events over the inbound
API.

## A separate endpoint for the agent

The inbound API is its own service on its own port, not part of the console or
the read API. That port is the only thing to expose to an agent: it serves
nothing but the inbound calls, shares no browser origin with the console, and a
firewall can admit it alone. It is off by default:

```bash
# .env
CALSYNC_INBOUND_REPLICAS=1
CALSYNC_INBOUND_PORT=8732        # optional
CALSYNC_INBOUND_BIND=0.0.0.0     # optional: the one host address to listen on
```

Add the team in the console under "Or: a tool sends the schedule". The page
shows the team's source id and key once; give both to the agent.

## Point the agent at the API docs

The service documents itself, with no key needed. Give the agent one of these
and it has everything it needs to send events correctly:

| | |
|---|---|
| `/inbound/v1/openapi.json` | OpenAPI 3.1, for an agent platform that imports a spec as tools |
| `/inbound/v1/docs` | The same for a person: every call with a ready-to-paste `curl` |

Both are generated from `src/calsync/inbound/contract.py` and tested against the
routes, so they cannot drift from what the service accepts.
