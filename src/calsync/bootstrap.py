"""Generate the credentials a first run needs, so nobody has to type them.

The manual first run asked for four things a person cannot get wrong quietly:
an `htpasswd -B` invocation, a hand-written JSON file, `chmod 600`, and — on
Linux only — `chown 10001`. None of them is a decision. The Radicale password
is machine-to-machine: calsync is the only thing that ever sends it, and no
human reads it. A value nobody chooses is a value nobody should have to make up.

So this makes them, once, and never again: **the users file existing is the
signal that auth is already established**. Regenerating it would rotate a
password every subscribed phone is still using, which is the one failure worse
than the friction being removed here.

The reader password *is* one a person types — into a phone's CalDAV settings,
or into Radicale's own web UI to look at what calsync has written — so it is
generated too and kept in the secret store beside the writer's. calsync never
reads it. Minting a credential with nowhere to look it up afterwards would be
worse than not minting it at all. It stays read-only: nothing but calsync
writes to that server, and a credential sitting on a family's phones should not
be able to delete a collection.

**A password set in the environment wins, and keeps winning.** Deployments that
would rather choose their own — or keep them in whatever already holds the rest
of a homelab's secrets — set `CALSYNC_SECRET_RADICALE_PASSWORD` (and
`..._READER_PASSWORD`) and this hashes those instead. It re-derives the users
file whenever the two disagree, so changing the variable and restarting is all
that a password change takes. Supplied values are never copied into the secrets
file: somebody who deliberately kept a credential out of a file on disk did not
ask for it to be written to one.

bcrypt is imported lazily and only here. It is not a runtime dependency of the
library (`pyproject.toml` has three, and this adds nothing to the sync path);
the published image installs it so this command works out of the box, and
anywhere it is missing this says so and points at `htpasswd`, which produces a
byte-identical file.
"""

from __future__ import annotations

import os
import secrets as _secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .secrets import SecretError, SecretStore, env_name

#: Where the deployment assets live inside the image, and the same files in a
#: checkout. Both, so this works whether you pulled the image or cloned.
DEPLOY_ASSETS = (
    Path("/app/deploy-assets"),
    Path(__file__).resolve().parent.parent.parent,
)

#: The account the calsync image runs as. A Linux bind mount preserves host
#: ownership, so a secrets file written by root is unreadable by the poller
#: unless it is handed over explicitly.
CALSYNC_UID = 10001

#: Secret refs. `radicale_secret_ref` defaults to the first of these, and the
#: second is stored for a human rather than for calsync.
WRITER_REF = "radicale_password"
READER_REF = "radicale_reader_password"

#: The read API's bearer token. `db.DEFAULT_SETTINGS["api_token_ref"]` names the
#: same string; the API refuses to serve without it.
API_REF = "api_token"

WRITER_USER = "calsync"
READER_USER = "calreader"


class BootstrapError(RuntimeError):
    """Something a person has to resolve before the stack can come up."""


@dataclass
class Result:
    lines: list[str] = field(default_factory=list)
    #: Printed once, because a reader password nobody sees cannot subscribe a
    #: phone. Empty when nothing was generated.
    reader_password: str = ""
    #: Same, for the read API's bearer token: generated here, and there is no
    #: other way to read it back out.
    api_token: str = ""

    def say(self, line: str) -> None:
        self.lines.append(line)


def generate_password() -> str:
    """A machine credential, not a memorable one.

    URL-safe because it goes into a CalDAV URL on a phone at least once, and
    a password that survives being pasted is worth more here than one that
    survives being typed.
    """
    return _secrets.token_urlsafe(24)


def htpasswd_entry(user: str, password: str) -> str:
    """One bcrypt line, exactly as `htpasswd -B` would write it.

    Not plaintext, even on a private network — `deploy/radicale/config` sets
    `htpasswd_encryption = bcrypt` and a plaintext entry would be accepted by
    the server, which is how that choice gets silently undone.
    """
    try:
        import bcrypt
    except ImportError as exc:  # pragma: no cover - exercised by hand, not CI
        raise BootstrapError(
            "bcrypt is not installed, so this cannot hash a password. Either "
            "install it (`pip install bcrypt`), or write the file yourself:\n"
            f"  htpasswd -B -c config/radicale/users {WRITER_USER}\n"
            f"  htpasswd -B    config/radicale/users {READER_USER}"
        ) from exc
    return f"{user}:{bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()}"


def _write(path: Path, text: str, mode: int) -> None:
    """Atomic, with the mode set before any content exists.

    Same reasoning as `SecretStore.put`: the window between creating a
    world-readable file and chmod-ing it is exactly long enough to lose
    something.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def _assets(kind: str = "radicale") -> Path:
    source = next((p for p in DEPLOY_ASSETS if (p / "deploy" / "radicale").is_dir()), None)
    if source is None:
        raise BootstrapError("this build carries no deployment assets")
    return source / "deploy" / kind


def _place(src: Path, out: Path, result: Result) -> None:
    """Copy one asset into a deployment, once.

    Never overwritten. These are files somebody edits — the rights file in
    particular — and replacing an edited one during a routine `up -d` is how a
    deployment loses a change nobody remembers making.
    """
    if out.exists():
        result.say(f"kept  {out}")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, out)
    os.chmod(out, 0o644)
    result.say(f"wrote {out}")


def ensure_server_config(config_dir: Path, result: Result) -> None:
    """Radicale's own config and rights file, if they are not there already."""
    assets = _assets()
    for name in ("config", "rights"):
        _place(assets / name, config_dir / name, result)


def ensure_proxy_config(config_dir: Path, result: Result) -> None:
    """The Caddyfile behind the stack's one published port.

    Here rather than left to the compose file to bind-mount out of a checkout,
    because a pulled deployment has no checkout — the image carries its own
    deployment assets and this is the same path Radicale's config takes. A
    proxy that came up with no config would serve nothing at the only address
    anybody has.
    """
    _place(_assets("caddy") / "Caddyfile", config_dir / "Caddyfile", result)


def _supplied(ref: str) -> str:
    """A password handed to us by the environment, if there is one.

    Read here rather than through ``SecretStore.get`` because the *origin*
    decides what happens next: a value from the environment is authoritative
    and stays there, while one from the file is ours to have written.
    """
    return os.environ.get(env_name(ref), "")


def _password_for(store: SecretStore, ref: str, result: Result) -> tuple[str, str]:
    """The password for one account, and where it came from.

    Environment, then the secret store, then a new one. The middle case is what
    lets a users file be deleted and rebuilt without invalidating the password
    calsync already has — the two are derived from one value rather than
    generated in pairs and hoped to match.
    """
    value = _supplied(ref)
    if value:
        # A deployment that generated a password and later moved to a variable
        # leaves the old one behind, and it is now wrong: it matches nothing in
        # the users file. Unset the variable one day and the store's copy is
        # what gets used, which is a 401 that looks like nothing changed. Not
        # deleted here — this does not take away a credential somebody may be
        # keeping deliberately — but said out loud, once, while it is cheap.
        # The file, not `get`, which prefers the environment and would compare
        # the supplied value against itself.
        stale = store.stored(ref)
        if stale and stale != value:
            result.say(
                f"note: {env_name(ref)} is set and {store.path} holds a different "
                f"{ref}. The variable wins while it is set; unsetting it would "
                f"fall back to a password that no longer works. Remove the "
                f"{ref!r} entry, or keep the variable."
            )
        return value, "environment"
    try:
        return store.get(ref), "store"
    except SecretError:
        return generate_password(), "generated"


def _entries(text: str) -> dict[str, str]:
    """user -> hash, preserving anything we did not put there.

    Somebody may have added a third account by hand. Rewriting the file from
    only the two accounts this knows about would silently delete it.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            user, _, digest = line.partition(":")
            out[user] = digest
    return out


def _matches(digest: str, password: str) -> bool:
    import bcrypt

    try:
        return bcrypt.checkpw(password.encode(), digest.encode())
    except ValueError:
        # A hash in some other format — a plaintext or md5 entry from an older
        # deployment. Treat it as a mismatch and rewrite it as bcrypt, which is
        # what the server config says it is.
        return False


def ensure_credentials(
    config_dir: Path,
    store: SecretStore,
    result: Result,
) -> None:
    """Make the users file agree with the passwords, whoever chose them.

    The two halves have to match — a users file with no matching secret
    authenticates nothing, and a secret with no users file is a password for an
    account that does not exist — so this derives one from the other every run
    rather than writing both once and trusting them to stay in step.

    Nothing is rotated: a password already in the store or the environment is
    the input, not something to replace. The only value invented here is one
    that does not exist anywhere yet.
    """
    users = config_dir / "users"
    existing = _entries(users.read_text()) if users.exists() else {}

    passwords, origins = {}, {}
    for user, ref in ((WRITER_USER, WRITER_REF), (READER_USER, READER_REF)):
        passwords[user], origins[user] = _password_for(store, ref, result)

    stale = [u for u, p in passwords.items()
             if u not in existing or not _matches(existing[u], p)]
    if not stale:
        result.say(f"kept  {users} (both accounts already match)")
        return

    # Store first: a users file whose password is nowhere to be found locks out
    # the poller, and the reverse — a stored password with no users file — is
    # simply retried on the next run.
    for user in stale:
        if origins[user] != "generated":
            continue
        try:
            store.put(WRITER_REF if user == WRITER_USER else READER_REF, passwords[user])
        except SecretError as exc:
            raise BootstrapError(str(exc)) from exc
        result.say(f"wrote {store.path} ({user}'s password)")

    merged = dict(existing)
    for user in stale:
        merged[user] = htpasswd_entry(user, passwords[user]).partition(":")[2]
    # 0644: these are bcrypt hashes, and the file has to be readable by whatever
    # uid the Radicale image ends up running as — which its own UID/GID
    # environment can change. Chowning it to a uid guessed here is a 401 nobody
    # can explain; `htpasswd` writes 0644 for the same reason.
    _write(users, "".join(f"{u}:{d}\n" for u, d in merged.items()), 0o644)
    result.say(
        f"wrote {users} ("
        + ", ".join(f"{u} from {origins[u] if origins[u] != 'store' else 'the secret store'}"
                    for u in stale)
        + ")"
    )

    # Only worth printing when nobody chose it — a password from a .env file is
    # already in front of the person reading the log.
    if READER_USER in stale and origins[READER_USER] == "generated":
        result.reader_password = passwords[READER_USER]


def ensure_api_token(store: SecretStore, result: Result) -> None:
    """The read API's bearer token, generated if nobody chose one.

    Same rules as the calendar passwords: an existing value is the input rather
    than something to replace, and one supplied by the environment is not copied
    into the secrets file. The API refuses to serve without a token, so
    generating it is what lets the service be on by default.
    """
    token, origin = _password_for(store, API_REF, result)
    if origin != "generated":
        result.say(f"kept  {store.path} (api_token from "
                   f"{'the environment' if origin == 'environment' else 'the secret store'})")
        return
    try:
        store.put(API_REF, token)
    except SecretError as exc:
        raise BootstrapError(str(exc)) from exc
    result.say(f"wrote {store.path} (the read API's token)")
    result.api_token = token


def _hand_over(path: Path, uid: int, result: Result) -> None:
    """Give the secrets file to the account calsync runs as.

    The step that bit three times. A Linux bind mount preserves host ownership,
    so a 600 file written by root here is unreadable by uid 10001 in the poller
    — and macOS hides it completely, presenting every mount as the container
    user. Doing it while we are root is the only moment nobody has to remember.
    """
    try:
        os.chown(path, uid, -1)
    except (PermissionError, OSError) as exc:
        # Not fatal: on macOS it is unnecessary, and running this as the same
        # account that runs calsync is a perfectly good arrangement.
        result.say(f"note: could not chown {path} to uid {uid} ({exc}); "
                   f"fine if calsync runs as the owner already")


def run(root: Path, *, store: SecretStore | None = None,
        owner_uid: int | None = CALSYNC_UID) -> Result:
    """Lay out server config and credentials under ``root``. Idempotent."""
    root = Path(root)
    result = Result()
    store = store or SecretStore(path=root / "secrets" / "secrets.json")
    ensure_server_config(root / "config" / "radicale", result)
    ensure_proxy_config(root / "config" / "caddy", result)
    ensure_credentials(root / "config" / "radicale", store, result)
    ensure_api_token(store, result)
    # Last, and here rather than inside a step: every secret is written by now,
    # and `SecretStore.put` writes a new file each time, so a handover done
    # earlier would be undone by the next thing to store something.
    if owner_uid is not None and store.path.exists():
        _hand_over(store.path, owner_uid, result)
    return result
