from __future__ import annotations

import contextlib
import os
import re
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ENV_FILE_VAR = "OPAL_DOTENV_FILE"
NO_ENV_FILE_VAR = "OPAL_NO_DOTENV"
_SECRET_NAME = re.compile(r"(?:TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY)", re.IGNORECASE)


class DotenvError(ValueError):
    """Raised for an invalid explicit dotenv policy or file."""


@dataclass(frozen=True)
class DotenvResult:
    path: Path | None
    loaded: bool
    created: bool
    disabled: bool


def _line(key: str, value: object) -> str | None:
    if _SECRET_NAME.search(key):
        return f"# {key}=<redacted>"
    text = str(value)
    if "\n" in text or "\r" in text or "'" in text:
        return f"# {key}=<omitted: value cannot be represented safely>"
    return f"{key}='{text}'"


def create_default_env(path: Path, values: Mapping[str, object]) -> bool:
    lines = [
        "# Generated OPAL environment snapshot.",
        "# Existing environment variables always override these values.",
        "# This file is not synchronized automatically; active entries take",
        "# precedence over later configuration-file changes.",
    ]
    lines.extend(line for key, value in values.items() if (line := _line(key, value)))
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return True


def _resolve(path: str | os.PathLike[str], cwd: Path) -> Path:
    value = Path(path).expanduser()
    return value if value.is_absolute() else cwd / value


def _readable(path: Path) -> bool:
    try:
        with path.open("rb"):
            return path.is_file()
    except OSError:
        return False


def prepare(
    *,
    env_file: str | os.PathLike[str] | None = None,
    no_env_file: bool = False,
    baseline: Mapping[str, object] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> DotenvResult:
    """Apply the OPAL dotenv policy for an executable startup path.

    Library callers that do not invoke this function see no dotenv file access,
    creation, or environment mutation. ``override=False`` preserves all existing
    environment variables, including empty strings.
    """
    root = Path(cwd or Path.cwd()).resolve()
    if env_file and no_env_file:
        raise DotenvError("--env-file and --no-env-file are mutually exclusive")
    inherited_disabled = os.environ.get(NO_ENV_FILE_VAR) is not None
    inherited_file = os.environ.get(ENV_FILE_VAR)
    disabled = bool(no_env_file or (env_file is None and inherited_disabled))
    if disabled:
        os.environ[NO_ENV_FILE_VAR] = "1"
        os.environ.pop(ENV_FILE_VAR, None)
        return DotenvResult(path=None, loaded=False, created=False, disabled=True)

    created = False
    explicit = env_file is not None or inherited_file is not None
    path = _resolve(env_file or inherited_file or ".env", root).resolve()
    if explicit:
        if not path.is_file():
            raise DotenvError(f"dotenv file not found: {path}")
        if not _readable(path):
            raise DotenvError(f"dotenv file is not readable: {path}")
    else:
        if not path.exists():
            try:
                created = create_default_env(path, baseline or {})
            except OSError as exc:
                warnings.warn(f"could not initialize {path}: {exc}", RuntimeWarning, stacklevel=2)
                created = False
        else:
            created = False
            if not _readable(path):
                warnings.warn(f"dotenv file is not readable: {path}", RuntimeWarning, stacklevel=2)
                loaded = False
                os.environ[ENV_FILE_VAR] = str(path)
                os.environ.pop(NO_ENV_FILE_VAR, None)
                return DotenvResult(path=path, loaded=loaded, created=False, disabled=False)

    loaded = load_dotenv(path, override=False)
    os.environ[ENV_FILE_VAR] = str(path)
    os.environ.pop(NO_ENV_FILE_VAR, None)
    return DotenvResult(path=path, loaded=loaded, created=created, disabled=False)
