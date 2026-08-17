"""Splitting a credential out of a feed URL.

A feed URL handed over by a team app is a bearer capability: it reads a child's
schedule and the physical places they will be, with timestamps. It has to end up
in the secret store and not in ``sources.url_template``, and the templating has
to be exactly reversible — a URL that is silently mangled here shows up weeks
later as a source that has simply stopped working, with nothing to point at.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from calsync.fetch import render_url
from calsync.onboarding import OnboardingError, analyse_url, templatise

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)


class Store:
    def __init__(self, values):
        self.values = values

    def get(self, ref):
        return self.values[ref]


def roundtrip(template, secrets):
    return render_url(template, secrets=Store(secrets), now=NOW).url


# --- what looks like a credential -------------------------------------------


def test_a_query_parameter_named_like_a_token_is_proposed():
    plan = analyse_url("https://api.example.com/v1/ics?token=abc123&group_ids=68362")
    assert plan.recommended == "params"
    assert plan.recommended_keys == ("query:token",)


def test_an_opaque_path_is_proposed_when_there_is_no_query():
    plan = analyse_url("https://teamreach.example/ics/9f3c1ab27e4d55c0.ics")
    assert plan.recommended == "path"
    assert plan.recommended_keys == ("path",)


def test_an_ordinary_path_is_not_proposed():
    plan = analyse_url("https://example.com/calendars/team.ics")
    assert plan.recommended == "none"


def test_a_value_is_never_shown_in_full():
    plan = analyse_url("https://api.example.com/ics?token=supersecretvalue")
    previews = [p.preview for p in plan.parts]
    assert not any("supersecretvalue" in p for p in previews)
    assert previews[0] == "su••••••ue", "unrecognisable is no use either"


# --- templating is reversible ------------------------------------------------


def test_a_query_token_round_trips():
    url = "https://api.example.com/v1/ics.ics?token=abc123&from=1&group_ids=68362"
    template, secrets = templatise(url, ["query:token"], "p360")

    assert "abc123" not in template
    assert secrets == {"p360_token": "abc123"}
    assert roundtrip(template, secrets) == url


def test_a_path_token_round_trips_and_keeps_the_extension():
    url = "https://teamreach.example/ics/9f3c1ab27e4d55c0.ics"
    template, secrets = templatise(url, ["path"], "tr_hawks")

    assert template == "https://teamreach.example/ics/{{secret:tr_hawks}}.ics"
    assert roundtrip(template, secrets) == url


def test_a_percent_encoded_value_is_not_double_encoded():
    """``render_url`` encodes what it substitutes, so the stored value must not be.

    Store the raw ``a%2Fb`` and every fetch would ask for ``a%252Fb``.
    """
    url = "https://api.example.com/ics?token=a%2Fb%2Bc"
    template, secrets = templatise(url, ["query:token"], "s")

    assert secrets["s_token"] == "a/b+c"
    assert roundtrip(template, secrets) == url


def test_an_empty_parameter_keeps_its_equals_sign():
    url = "https://api.example.com/ics?token=abc&debug=&v=2"
    template, secrets = templatise(url, ["query:token"], "s")
    assert roundtrip(template, secrets) == url


def test_several_parameters_get_one_ref_each():
    url = "https://api.example.com/ics?token=abc&access_key=def"
    template, secrets = templatise(url, ["query:token", "query:access_key"], "s")

    assert set(secrets) == {"s_token", "s_access_key"}
    assert roundtrip(template, secrets) == url


def test_vaulting_nothing_leaves_the_url_alone():
    url = "https://example.com/calendars/team.ics"
    assert templatise(url, [], "s") == (url, {})


def test_a_ref_name_that_contains_the_value_is_not_a_leak():
    """The refs come from the team name, so they often contain the path stem.

    ``/ics/hawks`` vaulted under ``tr_hawks_spring`` leaves ``hawks`` inside the
    placeholder — which is the placeholder doing its job, not a leaked value.
    """
    url = "https://teamreach.example/ics/hawks"
    template, secrets = templatise(url, ["path"], "tr_hawks_spring")

    assert template == "https://teamreach.example/ics/{{secret:tr_hawks_spring}}"
    assert roundtrip(template, secrets) == url


def test_a_url_that_would_not_survive_templating_is_refused():
    """Refusing beats saving something that fetches a different URL."""
    with pytest.raises(OnboardingError):
        templatise("ftp://example.com/feed.ics", ["path"], "s")
