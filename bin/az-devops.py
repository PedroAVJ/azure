#!/usr/bin/env python3
"""Run Azure DevOps CLI commands with an isolated cached Microsoft identity."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Iterator, TextIO
from urllib.parse import unquote, urlsplit

import msal


DEFAULT_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
DEFAULT_AUTHORITY = "https://login.microsoftonline.com/organizations"
DEFAULT_SCOPES = ("499b84ac-1321-427f-aa17-267ca6975798/.default",)
MAX_GIT_CREDENTIAL_INPUT = 16_384


@dataclass(frozen=True)
class DevOpsProfile:
    username: str
    config_dir: Path
    extension_dir: Path
    token_cache: Path
    authority: str


def _expand_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


def _config_path() -> Path:
    explicit_path = os.environ.get("AZURE_PLUGIN_CONFIG")
    if explicit_path:
        return _expand_path(explicit_path)

    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = _expand_path(config_home) if config_home else Path.home() / ".config"
    return base / "azure-plugin" / "profiles.json"


def _read_document() -> dict:
    path = _config_path()
    if not path.exists():
        return {}

    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read Azure profile configuration: {error}") from error

    if not isinstance(document, dict):
        raise RuntimeError("Azure profile configuration must contain a JSON object.")
    return document


def _write_document(document: dict) -> None:
    path = _config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as temporary_file:
            json.dump(document, temporary_file, indent=2, sort_keys=True)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _profile() -> DevOpsProfile:
    document = _read_document()
    configured = document.get("devops", {})
    if not isinstance(configured, dict):
        raise RuntimeError("The devops profile must contain a JSON object.")

    username = os.environ.get("AZURE_DEVOPS_USERNAME") or configured.get("username")
    if not username:
        raise RuntimeError("Azure DevOps is not configured. Run: az-devops configure --username ACCOUNT")

    config_dir = _expand_path(
        os.environ.get("AZURE_DEVOPS_CONFIG_DIR")
        or configured.get("config_dir")
        or Path.home() / ".azure-devops"
    )
    extension_dir = _expand_path(
        os.environ.get("AZURE_DEVOPS_EXTENSION_DIR")
        or configured.get("extension_dir")
        or Path.home() / ".azure" / "cliextensions"
    )
    token_cache = _expand_path(
        os.environ.get("AZURE_DEVOPS_TOKEN_CACHE")
        or configured.get("token_cache")
        or config_dir / "msal_token_cache.json"
    )
    authority = (
        os.environ.get("AZURE_DEVOPS_AUTHORITY")
        or configured.get("authority")
        or DEFAULT_AUTHORITY
    )

    return DevOpsProfile(
        username=str(username),
        config_dir=config_dir,
        extension_dir=extension_dir,
        token_cache=token_cache,
        authority=str(authority),
    )


def _load_cache(path: Path) -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if path.exists():
        cache.deserialize(path.read_text())
    return cache


def _save_cache(path: Path, cache: msal.SerializableTokenCache) -> None:
    if not cache.has_state_changed:
        return

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as temporary_file:
            temporary_file.write(cache.serialize())
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


@contextmanager
def _locked_application(
    profile: DevOpsProfile,
) -> Iterator[tuple[msal.SerializableTokenCache, msal.PublicClientApplication]]:
    profile.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = profile.token_cache.with_suffix(".lock")
    with lock_path.open("a+") as lock_file:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        cache = _load_cache(profile.token_cache)
        application = msal.PublicClientApplication(
            DEFAULT_CLIENT_ID,
            authority=profile.authority,
            token_cache=cache,
        )
        try:
            yield cache, application
        finally:
            _save_cache(profile.token_cache, cache)


def _account(application: msal.PublicClientApplication, username: str) -> dict | None:
    accounts = application.get_accounts(username=username)
    if accounts:
        return accounts[0]

    expected = username.casefold()
    return next(
        (
            account
            for account in application.get_accounts()
            if str(account.get("username", "")).casefold() == expected
        ),
        None,
    )


def _silent_token(application: msal.PublicClientApplication, account: dict) -> dict | None:
    return application.acquire_token_silent_with_error(list(DEFAULT_SCOPES), account=account)


def _git_path(value: str) -> tuple[str, ...] | None:
    """Normalize one Azure repository path without accepting path traversal."""
    if value.startswith("/"):
        value = value[1:]
    try:
        parts = tuple(unquote(part, errors="strict") for part in value.split("/"))
    except (UnicodeError, ValueError):
        return None
    if len(parts) not in (3, 4) or parts[-2] != "_git":
        return None
    if any(not part or part in (".", "..") or any(
        character in "/\\" or ord(character) < 32 or ord(character) == 127
        for character in part
    ) for part in parts):
        return None
    return parts


def _configured_git_path(url: str) -> tuple[str, ...] | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if (parsed.scheme != "https" or parsed.netloc.casefold() != "dev.azure.com"
            or parsed.query or parsed.fragment):
        return None
    return _git_path(parsed.path)


def _read_git_request(stream: TextIO) -> tuple[dict[str, str], set[str]]:
    values: dict[str, str] = {}
    capabilities: set[str] = set()
    total = 0
    while True:
        line = stream.readline(MAX_GIT_CREDENTIAL_INPUT + 1)
        total += len(line)
        if total > MAX_GIT_CREDENTIAL_INPUT:
            raise ValueError("Credential request exceeds the bounded protocol size")
        if not line or line in ("\n", "\r\n"):
            return values, capabilities
        line = line.rstrip("\n")
        if "\r" in line or "\0" in line or "=" not in line:
            raise ValueError("Invalid credential protocol input")
        key, value = line.split("=", 1)
        if key == "capability[]":
            capabilities.add(value)
        elif key in ("protocol", "host", "path"):
            if key in values:
                raise ValueError("Duplicate credential protocol field")
            values[key] = value


def _git_credential(arguments: list[str]) -> int:
    """Speak only Git's scoped bearer protocol; never print this live output."""
    if arguments == ["capability"]:
        print("version 0\ncapability authtype")
        return 0
    if arguments not in (["get"], ["store"], ["erase"]):
        print("Use this helper through Git's credential interface.", file=sys.stderr)
        return 2
    # Store and erase deliberately do not read, print, persist, or revoke the
    # credential Git offers. Provider cache ownership remains with Azure/MSAL.
    if arguments[0] != "get":
        return 0
    try:
        request, capabilities = _read_git_request(sys.stdin)
        if request.get("protocol") != "https" or request.get("host", "").casefold() != "dev.azure.com":
            return 0
        path = _git_path(request.get("path", ""))
        if path is None:
            return 0
        configured = _read_document().get("devops", {})
        urls = configured.get("git_urls", []) if isinstance(configured, dict) else []
        if not isinstance(urls, list) or not any(
            isinstance(url, str) and _configured_git_path(url) == path for url in urls
        ):
            return 0
        if "authtype" not in capabilities:
            print("quit=true\n")
            print("Azure Git authentication requires Git's authtype capability.", file=sys.stderr)
            return 1
        # Suppress provider chatter and exception details. Only the credential
        # protocol below may leave stdout, and only directly to the Git child.
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            profile = _profile()
            with _locked_application(profile) as (_, application):
                account = _account(application, profile.username)
                result = _silent_token(application, account) if account else None
        token = result.get("access_token") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token or any(ord(c) <= 32 or ord(c) == 127 for c in token):
            raise RuntimeError("Silent session unavailable")
        # OAuth access tokens are Bearer credentials, never a PAT/password guess.
        # Git uses authtype + credential as its HTTP Authorization header.
        sys.stdout.write("capability[]=authtype\nauthtype=bearer\ncredential=" + token + "\nephemeral=true\n\n")
        return 0
    except Exception:
        print("quit=true\n")
        print("Azure Git authentication could not reuse the configured silent session.", file=sys.stderr)
        return 1


def _configure(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="az-devops configure")
    parser.add_argument("--username", required=True, help="Microsoft account name used by Azure DevOps")
    parser.add_argument("--config-dir", default="~/.azure-devops", help="Isolated Azure CLI config directory")
    parser.add_argument(
        "--extension-dir",
        default="~/.azure/cliextensions",
        help="Directory containing the Azure DevOps CLI extension",
    )
    parser.add_argument("--authority", default=DEFAULT_AUTHORITY, help="Microsoft identity authority")
    parser.add_argument("--git-url", action="append", help="Exact HTTPS dev.azure.com repository URL allowed for the Git helper; repeat for additional repositories")
    parsed = parser.parse_args(arguments)

    if parsed.git_url and any(_configured_git_path(url) is None for url in parsed.git_url):
        parser.error("--git-url must be an exact HTTPS dev.azure.com repository URL without credentials or query parameters")

    document = _read_document()
    previous = document.get("devops", {})
    previous_git_urls = previous.get("git_urls", []) if isinstance(previous, dict) else []
    git_urls = parsed.git_url if parsed.git_url is not None else previous_git_urls
    document["devops"] = {
        "authority": parsed.authority,
        "config_dir": parsed.config_dir,
        "extension_dir": parsed.extension_dir,
        "username": parsed.username,
    }
    if git_urls:
        document["devops"]["git_urls"] = git_urls
    _write_document(document)
    _expand_path(parsed.config_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
    print(f"Azure DevOps profile configured for {parsed.username}.")
    print("No credential or token was written to the plugin configuration.")
    return 0


def _login(profile: DevOpsProfile, force: bool) -> int:
    with _locked_application(profile) as (_, application):
        account = _account(application, profile.username)
        if account and not force:
            result = _silent_token(application, account)
            if result and result.get("access_token"):
                print(f"Azure DevOps is already authenticated as {profile.username}.")
                return 0

        flow = application.initiate_device_flow(scopes=list(DEFAULT_SCOPES))
        if "user_code" not in flow:
            print("Could not start Microsoft device authentication.", file=sys.stderr)
            return 1

        print(flow.get("message", "Complete Microsoft device authentication."), flush=True)
        result = application.acquire_token_by_device_flow(flow)
        if result.get("access_token"):
            print(f"Azure DevOps authentication saved for {profile.username}.")
            return 0

        error = result.get("error", "authentication_failed")
        print(f"Microsoft authentication failed: {error}", file=sys.stderr)
        return 1


def _status(profile: DevOpsProfile) -> int:
    with _locked_application(profile) as (_, application):
        account = _account(application, profile.username)
        if not account:
            print(f"Azure DevOps account is not present in the cache: {profile.username}")
            print("Run: az-devops login")
            return 1

        result = _silent_token(application, account)
        if result and result.get("access_token"):
            print(f"Azure DevOps session is valid: {profile.username}")
            print(f"Config directory: {profile.config_dir}")
            return 0

        error = result.get("error", "interaction_required") if result else "interaction_required"
        print(f"Azure DevOps session needs renewal: {error}")
        print("Run: az-devops login")
        return 1


def _run_az(profile: DevOpsProfile, arguments: list[str]) -> int:
    with _locked_application(profile) as (_, application):
        account = _account(application, profile.username)
        if not account:
            print("The selected Microsoft account is absent from the cached session.", file=sys.stderr)
            print("Run: az-devops login", file=sys.stderr)
            return 1
        result = _silent_token(application, account)

    token = result.get("access_token") if result else None
    if not token:
        error = result.get("error", "interaction_required") if result else "interaction_required"
        print(f"The cached Azure DevOps session needs renewal ({error}).", file=sys.stderr)
        print("Run: az-devops login", file=sys.stderr)
        return 1

    az_cli = os.environ.get("AZURE_CLI_BIN") or shutil.which("az")
    if not az_cli:
        print("Azure CLI was not found on PATH.", file=sys.stderr)
        return 127

    environment = os.environ.copy()
    environment.update(
        {
            "AZURE_CONFIG_DIR": str(profile.config_dir),
            "AZURE_EXTENSION_DIR": str(profile.extension_dir),
            "AZURE_DEVOPS_EXT_PAT": token,
        }
    )
    os.execve(az_cli, [az_cli, *arguments], environment)
    return 1


def main() -> int:
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "configure":
        return _configure(arguments[1:])
    if arguments and arguments[0] == "git-credential":
        return _git_credential(arguments[1:])

    try:
        profile = _profile()
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 2

    if arguments and arguments[0] == "login":
        login_parser = argparse.ArgumentParser(prog="az-devops login")
        login_parser.add_argument("--force", action="store_true")
        parsed = login_parser.parse_args(arguments[1:])
        return _login(profile, force=parsed.force)

    if arguments and arguments[0] == "status":
        if len(arguments) != 1:
            print("Usage: az-devops status", file=sys.stderr)
            return 2
        return _status(profile)

    return _run_az(profile, arguments)


if __name__ == "__main__":
    raise SystemExit(main())
