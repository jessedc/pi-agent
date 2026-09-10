"""pi's model registry and defaults, as data.

`models.json` and `settings.json` are generated at container start from the
environment the host forwards, never checked in and COPYed into the image.
Baking them in would fix one tailnet host, one quantisation and one thinking
level into every build, and pointing the bot at a different model would mean
editing two JSON files, the agent definition, and rebuilding.

Generating them puts every value in one place -- `.env` -- and leaves nothing to
default to: a model this harness cannot fully describe is a failure at the first
second of a run rather than a confusing one several minutes in.

Both halves of the package depend on this module: the host forwards
`as_environ`, the container reads it back with `from_environ` and writes the
files. Sharing the names is what stops the two sides from drifting.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

# A JSON document as pi will read it back. `Any` rather than a nested TypedDict:
# these are two small literals, and the shape that matters is pi's, not ours.
Document = dict[str, Any]

MODELS_FILE = "models.json"
SETTINGS_FILE = "settings.json"

# Field name -> environment variable. One mapping, so `from_environ` and
# `as_environ` cannot disagree about what a value is called.
VARS: Mapping[str, str] = {
    "host": "MODEL_HOST",
    "port": "MODEL_PORT",
    "provider": "MODEL_PROVIDER",
    "api": "MODEL_API",
    "api_key": "MODEL_API_KEY",
    "model_id": "MODEL_ID",
    "model_name": "MODEL_NAME",
    "context_window": "MODEL_CONTEXT_WINDOW",
    "reasoning": "MODEL_REASONING",
    "supports_developer_role": "MODEL_SUPPORTS_DEVELOPER_ROLE",
    "thinking": "THINKING_LEVEL",
}


class MissingConfig(ValueError):
    """The model configuration is absent or unusable."""


def _boolean(var: str, raw: str) -> bool:
    """Parse a strict `true`/`false`, because JSON has no truthiness."""
    lowered = raw.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    raise MissingConfig(f"{var} must be true or false, got: {raw}")


def _integer(var: str, raw: str) -> int:
    """Parse a positive integer; a typo'd port or window is caught here."""
    try:
        value = int(raw.strip())
    except ValueError as error:
        raise MissingConfig(f"{var} must be a number, got: {raw}") from error
    if value <= 0:
        raise MissingConfig(f"{var} must be greater than zero, got: {raw}")
    return value


@dataclass(frozen=True, slots=True)
class Config:
    """Everything pi needs to know about the model it is talking to."""

    host: str
    port: int
    provider: str
    api: str
    api_key: str
    model_id: str
    model_name: str
    context_window: int
    reasoning: bool
    supports_developer_role: bool
    thinking: str

    @property
    def base_url(self) -> str:
        """The endpoint, composed rather than configured.

        The container reaches the host by name: the tailnet address is supplied
        as a `--add-host` alias, so the URL here is the same one a human would
        use on the tailnet.
        """
        return f"http://{self.host}:{self.port}/v1"

    @property
    def model_ref(self) -> str:
        """How `pi --model` names this model: provider-qualified."""
        return f"{self.provider}/{self.model_id}"

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> Config:
        """Read the configuration out of `environ`, or say what is missing.

        Every variable is reported at once. Fixing an eleven-variable `.env` one
        failed run at a time is the kind of thing that makes people go back to
        hardcoding.
        """
        missing = [var for var in VARS.values() if not environ.get(var, "").strip()]
        if missing:
            raise MissingConfig(f"not set: {', '.join(missing)}")

        raw = {field: environ[var].strip() for field, var in VARS.items()}
        return cls(
            host=raw["host"],
            port=_integer(VARS["port"], raw["port"]),
            provider=raw["provider"],
            api=raw["api"],
            api_key=raw["api_key"],
            model_id=raw["model_id"],
            model_name=raw["model_name"],
            context_window=_integer(VARS["context_window"], raw["context_window"]),
            reasoning=_boolean(VARS["reasoning"], raw["reasoning"]),
            supports_developer_role=_boolean(
                VARS["supports_developer_role"], raw["supports_developer_role"]
            ),
            thinking=raw["thinking"],
        )


def as_environ(config: Config) -> dict[str, str]:
    """The configuration as the environment variables it came from.

    `from_environ(as_environ(config)) == config`, which is what makes it safe
    for the host to forward the set without enumerating it a second time.
    """
    values: dict[str, str] = {}
    for field in fields(config):
        value = getattr(config, field.name)
        values[VARS[field.name]] = str(value).lower() if isinstance(value, bool) else str(value)
    return values


def models(config: Config) -> Document:
    """The `models.json` document: pi's registry, holding exactly one model."""
    return {
        "providers": {
            config.provider: {
                "api": config.api,
                "apiKey": config.api_key,
                "baseUrl": config.base_url,
                "compat": {"supportsDeveloperRole": config.supports_developer_role},
                "models": [
                    {
                        "contextWindow": config.context_window,
                        "id": config.model_id,
                        "name": config.model_name,
                        "reasoning": config.reasoning,
                    }
                ],
            }
        }
    }


def settings(config: Config) -> Document:
    """The `settings.json` document: pi's defaults for this container.

    `quietStartup` and `enableInstallTelemetry` are not configuration: a run is
    non-interactive and its stderr is the audit record, so the banner is noise
    and the telemetry call is an egress this container has no reason to make.
    """
    return {
        "defaultProvider": config.provider,
        "defaultModel": config.model_id,
        "defaultThinkingLevel": config.thinking,
        "quietStartup": True,
        "enableInstallTelemetry": False,
    }


def _write_private(path: Path, document: Document) -> None:
    """Write `document` readable by its owner alone.

    `models.json` carries the model API key in clear, so it is created with
    the mode it should have rather than chmod'd afterwards, which would leave
    a window with the default umask's mode. settings.json holds no secret and
    gets the same treatment: one code path, one set of properties to test.
    """
    text = json.dumps(document, indent=2) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    # O_CREAT's mode only applies to a new file; a stale one keeps its own.
    path.chmod(0o600)


def write(config: Config, directory: Path) -> tuple[Path, Path]:
    """Write both documents into pi's configuration directory, owner-only."""
    directory.mkdir(parents=True, exist_ok=True)

    models_path = directory / MODELS_FILE
    _write_private(models_path, models(config))

    settings_path = directory / SETTINGS_FILE
    _write_private(settings_path, settings(config))

    return models_path, settings_path
