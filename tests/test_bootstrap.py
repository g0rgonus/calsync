"""Credentials nobody has to type.

The first run used to ask for `htpasswd -B` twice, a hand-written JSON file,
`chmod 600`, and — on Linux only — `sudo chown 10001`, which bit three separate
times. None of it is a decision: the Radicale password is machine-to-machine
and no human ever reads it.

The risk in generating it is the opposite one, so most of what is tested here is
restraint: never rotating a password devices are already using, and never
clobbering a secrets file that holds a feed's token.
"""

from __future__ import annotations

import json
import stat

import pytest

from calsync import bootstrap
from calsync.secrets import SecretStore

pytest.importorskip("bcrypt", reason="the deploy extra; bootstrap says so itself")


@pytest.fixture
def root(tmp_path):
    return tmp_path / "deploy"


def _store(root):
    return SecretStore(path=root / "secrets" / "secrets.json")


def _run(root):
    # No chown: the tests do not run as root, and on macOS it is unnecessary.
    return bootstrap.run(root, store=_store(root), owner_uid=None)


def test_a_first_run_produces_a_working_set(root):
    result = _run(root)
    users = (root / "config" / "radicale" / "users").read_text()

    assert users.count("\n") == 2
    assert users.startswith("calsync:$2b$")
    assert "\ncalreader:$2b$" in users
    assert (root / "config" / "radicale" / "config").exists()
    assert (root / "config" / "radicale" / "rights").exists()
    assert result.reader_password


def test_the_stored_password_is_the_one_in_the_users_file(root):
    import bcrypt

    _run(root)
    stored = json.loads((root / "secrets" / "secrets.json").read_text())
    line = (root / "config" / "radicale" / "users").read_text().splitlines()[0]

    assert bcrypt.checkpw(stored["radicale_password"].encode(),
                          line.split(":", 1)[1].encode())


def test_it_never_rotates_a_password_devices_are_using(root):
    """The failure that would be worse than the friction being removed.

    A second `docker compose up -d` runs this again, and a fresh password would
    lock out every phone already subscribed — and calsync itself, whose stored
    copy would no longer match.
    """
    _run(root)
    before = (root / "config" / "radicale" / "users").read_bytes()
    secret_before = (root / "secrets" / "secrets.json").read_bytes()

    result = _run(root)

    assert (root / "config" / "radicale" / "users").read_bytes() == before
    assert (root / "secrets" / "secrets.json").read_bytes() == secret_before
    assert result.reader_password == ""
    assert any("already match" in line for line in result.lines)


def test_it_keeps_a_secrets_file_that_already_holds_a_feed_token(root):
    """The console writes feed tokens here, and losing one is unrecoverable.

    A pasted feed URL is a bearer credential that the app may not show twice, so
    this merges rather than writing the file it wants.
    """
    store = _store(root)
    store.put("p360_token", "a-real-token")

    bootstrap.run(root, store=store, owner_uid=None)
    stored = json.loads((root / "secrets" / "secrets.json").read_text())

    assert stored["p360_token"] == "a-real-token"
    assert "radicale_password" in stored


def test_the_secrets_file_is_never_readable_by_anyone_else(root):
    _run(root)
    mode = (root / "secrets" / "secrets.json").stat().st_mode
    assert not mode & (stat.S_IRGRP | stat.S_IROTH)


def test_the_users_file_is_readable_by_the_server_account(root):
    """0644 on purpose: bcrypt hashes, and Radicale's uid is its own choice.

    The image's UID/GID environment can remap the account it runs as, so a file
    chowned to a uid guessed here is a 401 nobody can explain.
    """
    _run(root)
    mode = (root / "config" / "radicale" / "users").stat().st_mode
    assert mode & stat.S_IROTH


def test_an_edited_rights_file_survives(root):
    """It is the one file in here somebody genuinely edits."""
    target = root / "config" / "radicale"
    target.mkdir(parents=True)
    (target / "rights").write_text("# mine\n")

    _run(root)

    assert (target / "rights").read_text() == "# mine\n"


def test_a_users_file_with_no_password_behind_it_is_repaired(root):
    """Half a state authenticates nothing, and nothing would ever report it.

    An entry whose password is in no store and no environment is one calsync
    cannot use, so it is replaced rather than trusted — the users file is
    derived from the passwords, not kept alongside them.
    """
    target = root / "config" / "radicale"
    target.mkdir(parents=True)
    (target / "users").write_text("calsync:$2b$12$whatever\n")

    _run(root)

    import bcrypt
    stored = json.loads((root / "secrets" / "secrets.json").read_text())
    entries = dict(
        line.split(":", 1) for line in (target / "users").read_text().splitlines()
    )
    assert bcrypt.checkpw(stored["radicale_password"].encode(),
                          entries["calsync"].encode())


def test_an_account_somebody_added_by_hand_survives(root):
    """Rewriting from only the two accounts it knows would delete the third."""
    target = root / "config" / "radicale"
    target.mkdir(parents=True)
    (target / "users").write_text("someone:$2b$12$theirs\n")

    _run(root)
    entries = (target / "users").read_text()

    assert "someone:$2b$12$theirs" in entries
    assert entries.count("\n") == 3


# --- passwords chosen before startup ----------------------------------------


def test_a_password_from_the_environment_is_the_one_hashed(root, monkeypatch):
    import bcrypt

    monkeypatch.setenv("CALSYNC_SECRET_RADICALE_PASSWORD", "chosen-by-me")
    _run(root)
    entries = dict(
        line.split(":", 1)
        for line in (root / "config" / "radicale" / "users").read_text().splitlines()
    )

    assert bcrypt.checkpw(b"chosen-by-me", entries["calsync"].encode())


def test_a_supplied_password_is_never_copied_into_the_secrets_file(root, monkeypatch):
    """Somebody who kept a credential out of a file did not ask for one.

    The environment stays the source of truth for it — which is also why the
    users file is re-derived when the two disagree, rather than being written
    once and trusted.
    """
    monkeypatch.setenv("CALSYNC_SECRET_RADICALE_PASSWORD", "chosen-by-me")
    _run(root)
    stored = json.loads((root / "secrets" / "secrets.json").read_text())

    assert "radicale_password" not in stored
    assert "radicale_reader_password" in stored   # this one nobody chose


def test_changing_the_variable_and_restarting_changes_the_password(root, monkeypatch):
    import bcrypt

    monkeypatch.setenv("CALSYNC_SECRET_RADICALE_PASSWORD", "first")
    _run(root)
    monkeypatch.setenv("CALSYNC_SECRET_RADICALE_PASSWORD", "second")
    result = _run(root)

    entries = dict(
        line.split(":", 1)
        for line in (root / "config" / "radicale" / "users").read_text().splitlines()
    )
    assert bcrypt.checkpw(b"second", entries["calsync"].encode())
    assert any("environment" in line for line in result.lines)


def test_a_stored_password_rebuilds_a_deleted_users_file(root):
    """The file is derivable, so losing it is not losing the deployment."""
    import bcrypt

    _run(root)
    stored = json.loads((root / "secrets" / "secrets.json").read_text())
    (root / "config" / "radicale" / "users").unlink()

    _run(root)
    entries = dict(
        line.split(":", 1)
        for line in (root / "config" / "radicale" / "users").read_text().splitlines()
    )

    assert bcrypt.checkpw(stored["radicale_password"].encode(),
                          entries["calsync"].encode())
    assert json.loads((root / "secrets" / "secrets.json").read_text()) == stored


def test_a_secrets_file_that_cannot_be_written_leaves_nothing_behind(root, monkeypatch):
    """The password is stored before the file that depends on it, not after.

    A users file whose password is nowhere locks the poller out; a stored
    password with no users file is just retried on the next run. When one of
    them has to fail, it has to be that one.
    """
    from calsync.secrets import SecretError

    store = _store(root)
    monkeypatch.setattr(
        store, "put",
        lambda *a, **k: (_ for _ in ()).throw(SecretError("read-only mount")),
    )

    with pytest.raises(bootstrap.BootstrapError, match="read-only"):
        bootstrap.run(root, store=store, owner_uid=None)

    assert not (root / "config" / "radicale" / "users").exists()


def test_the_passwords_survive_a_url(root):
    """The reader's goes into a CalDAV URL on a phone at least once."""
    from urllib.parse import quote

    password = bootstrap.generate_password()
    assert quote(password, safe="") == password
    assert len(password) >= 24


def test_a_leftover_generated_password_is_called_out(root, monkeypatch):
    """Unsetting the variable later would otherwise be a 401 with no cause.

    The store's copy is what `SecretStore` falls back to, and it matches
    nothing in the users file any more.
    """
    _run(root)                                        # generates and stores one
    monkeypatch.setenv("CALSYNC_SECRET_RADICALE_PASSWORD", "chosen-later")
    result = _run(root)

    assert any("no longer works" in line for line in result.lines)
    # Said, not done: taking away a stored credential is not this command's call.
    stored = json.loads((root / "secrets" / "secrets.json").read_text())
    assert stored["radicale_password"] != "chosen-later"


def test_nothing_is_said_when_the_variable_and_the_store_agree(root, monkeypatch):
    _run(root)
    stored = json.loads((root / "secrets" / "secrets.json").read_text())
    monkeypatch.setenv("CALSYNC_SECRET_RADICALE_PASSWORD", stored["radicale_password"])

    result = _run(root)

    assert not any("no longer works" in line for line in result.lines)
