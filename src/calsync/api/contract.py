"""What this API actually does, as data.

Served at `GET /v1` so an agent can check the contract before operating rather
than inferring it from a document it was trained on, a chat scrollback, or a
404. docs/API.md already argued for this and called it the machine copy: "Don't
rely on Hermes inferring the API from scrollback — that's unbounded and
unversioned."

**It is checked against the running app, not written alongside it.** A
hand-maintained page describing an HTTP API drifts from the API on the first
busy afternoon, and a contract that lies is worse than no contract — an agent
that trusts it fails in ways nobody can reproduce. `tests/test_api.py` asserts
this spec and `app.routes` describe each other exactly, in both directions: a
route with no entry fails, and an entry with no route fails.

**It says what is *not* built, too.** Half of docs/API.md is design contract
with no code behind it, and the honest way to serve that is explicitly, with a
501 and a reason, rather than letting an agent discover it by getting a 404 that
looks like a typo.
"""

from __future__ import annotations

#: Bumped when a shape changes in a way a client could notice. Not the package
#: version: an agent cares whether the contract moved, not whether a venue
#: parser was tidied.
CONTRACT_VERSION = "1.0"

#: One entry per route the app actually serves, keyed by (method, rule) exactly
#: as Bottle reports them.
ENDPOINTS: dict[tuple[str, str], dict] = {
    ("GET", "/v1"): {
        "summary": "This document. Check it before operating.",
        "returns": {
            "contract_version": "string",
            "endpoints": "array of {method, path, summary, ...}",
            "not_implemented": "array of {path, reason}",
        },
    },
    ("GET", "/v1/events"): {
        "summary": "Events calsync has written, as fields rather than a title.",
        "query": {
            "from": "RFC3339 or a bare date. Defaults to now.",
            "to": "RFC3339 or a bare date. Defaults to 14 days out.",
            "child": "child id",
            "activity": "activity id",
        },
        "returns": {
            "events": "array; `summary_rendered` is a render — never parse it",
            "sources": "freshness per source; `stale` means the data may lag",
            "retained_from": "present when the window was clamped to retention",
        },
        "notes": [
            "Always date-bounded; a span wider than the sync window is refused.",
            "Content is pruned below `sync_window_back_days`, so there is "
            "nothing to find further back.",
        ],
    },
    ("GET", "/v1/events/<uid:path>"): {
        "summary": "One event by uid, unbounded by date.",
        "returns": {"event": "as in /v1/events", "sources": "freshness"},
    },
    ("POST", "/v1/tasks/<task_id>/result"): {
        "summary": "Answer a question calsync asked. Stored, never applied.",
        "body": {
            "answer": "object; shape depends on the task type, see answer_shapes",
            "answered_by": "required; what produced this, e.g. 'hermes/1.4'",
            "rationale": "optional; why. Shown to the human who decides.",
        },
        "returns": {
            "state": "'answered'",
            "applied": "always false",
        },
        "notes": [
            "A human approves in the console. There is no parameter here that "
            "applies an answer, and no endpoint that does.",
            "Only tasks calsync actually dispatched can be answered; an "
            "unrecognised id is a 404.",
            "Re-answering an approved or rejected task is a 409.",
        ],
    },
}

#: What each task type's `answer` must contain. Served so a client corrects
#: itself from the contract rather than from a rejection.
ANSWER_SHAPES: dict[str, dict] = {
    "resolve_activity": {
        "question": "which of these names is our team?",
        "answer": {"alias": "Hawks"},
    },
    "classify_kind": {
        "question": "is this label a game or a practice?",
        "answer": {"label": "Skills Session", "is_game": False},
    },
    "normalize_venue": {
        "question": "where is this?",
        "answer": {
            "name": "Riverview Farm Park",
            "address": "optional",
            "same_as": "or the canonical name of a venue already known",
        },
        "notes": ["Never send coordinates. Venue resolution is server-side, and "
                  "an approved answer still leaves the pin unconfirmed."],
    },
}

#: Specified in docs/API.md and not built. Served, and returned as a 501 with
#: the reason, so an agent learns why rather than reading a 404 as a typo.
NOT_IMPLEMENTED: dict[str, str] = {
    "/v1/documents": (
        "Specified in docs/API.md. Not built: nothing stores document blobs "
        "yet, so there is nothing for a proposal to reference."
    ),
    "/v1/proposals": (
        "Specified in docs/API.md. Not built. This is the path for events "
        "extracted from a document rather than a feed; it needs the documents "
        "store above and a proposals table, and it does not need Matrix."
    ),
    "/v1/amendments": (
        "Specified in docs/API.md. Not built: amendments mutate already-"
        "published events and need the trust-rank overlay in docs/MATRIX.md §4, "
        "which does not exist."
    ),
    "/v1/tasks": (
        "Listing tasks is not built. Answering one is: see "
        "POST /v1/tasks/{id}/result."
    ),
}


def document() -> dict:
    """The contract, as it will be served."""
    return {
        "contract_version": CONTRACT_VERSION,
        "service": "calsync",
        "summary": (
            "Read events as structured fields, and answer questions calsync "
            "asked. Everything that changes the family's calendars is approved "
            "by a human in the console, not here."
        ),
        "endpoints": [
            {"method": method, "path": rule, **spec}
            for (method, rule), spec in sorted(ENDPOINTS.items(), key=lambda kv: kv[0][1])
        ],
        "answer_shapes": ANSWER_SHAPES,
        "not_implemented": [
            {"path": path, "reason": reason}
            for path, reason in sorted(NOT_IMPLEMENTED.items())
        ],
    }
