"""Tests for the generated pi configuration.

`models.json` and `settings.json` were checked-in files, so the only way a bad
one showed up was a container that started and then could not reach a model.
Generating them makes the documents assertable without a build, a daemon or a
model -- and makes "the host forwards exactly what the container requires" a
property rather than a convention.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from pi_agent import _piconfig

ENVIRON = {
    "MODEL_HOST": "model-box.example.ts.net",
    "MODEL_PORT": "8080",
    "MODEL_PROVIDER": "llamacpp",
    "MODEL_API": "openai-completions",
    "MODEL_API_KEY": "no-api-key",
    "MODEL_ID": "DeepSeek-V4-Flash-0731-UD-IQ2_M",
    "MODEL_NAME": "DeepSeek V4 Flash 0731 (GB10)",
    "MODEL_CONTEXT_WINDOW": "262144",
    "MODEL_REASONING": "true",
    "MODEL_SUPPORTS_DEVELOPER_ROLE": "false",
    "THINKING_LEVEL": "high",
}


# ------------------------------------------------------------------ reading


def test_every_variable_is_required() -> None:
    """Nothing defaults: an omitted variable is a stopped run, not a guess."""
    for name in ENVIRON:
        with pytest.raises(_piconfig.MissingConfig) as error:
            _piconfig.Config.from_environ({k: v for k, v in ENVIRON.items() if k != name})

        assert name in str(error.value)


def test_all_the_missing_variables_are_reported_at_once() -> None:
    """Eleven variables discovered one failed run at a time is unusable."""
    with pytest.raises(_piconfig.MissingConfig) as error:
        _piconfig.Config.from_environ({})

    reported = str(error.value)
    assert all(name in reported for name in ENVIRON)


def test_a_variable_set_to_whitespace_counts_as_unset() -> None:
    with pytest.raises(_piconfig.MissingConfig):
        _piconfig.Config.from_environ({**ENVIRON, "MODEL_ID": "   "})


@pytest.mark.parametrize("raw", ["yes", "1", "no", "off"])
def test_a_boolean_must_be_true_or_false(raw: str) -> None:
    """JSON has no truthiness, and `reasoning: "yes"` is not a boolean."""
    with pytest.raises(_piconfig.MissingConfig):
        _piconfig.Config.from_environ({**ENVIRON, "MODEL_REASONING": raw})


@pytest.mark.parametrize("raw", ["eighty-eighty", "0", "-1", "8080.0"])
def test_a_number_must_be_a_positive_integer(raw: str) -> None:
    """A typo'd port fails here rather than as a connection refused mid-run."""
    with pytest.raises(_piconfig.MissingConfig):
        _piconfig.Config.from_environ({**ENVIRON, "MODEL_PORT": raw})


def test_the_environment_round_trips() -> None:
    """What the host forwards is what the container reads back, exactly.

    Without this the two halves drift: a bool stringified as `True` reads back
    as neither true nor false, and nothing notices until a container starts.
    """
    config = _piconfig.Config.from_environ(ENVIRON)

    assert _piconfig.as_environ(config) == ENVIRON
    assert _piconfig.Config.from_environ(_piconfig.as_environ(config)) == config


# ----------------------------------------------------------------- documents


def test_the_base_url_is_composed_from_the_host_and_port() -> None:
    """The container reaches the model by name; --add-host supplies the address."""
    config = _piconfig.Config.from_environ(ENVIRON)

    assert config.base_url == "http://model-box.example.ts.net:8080/v1"
    assert config.model_ref == "llamacpp/DeepSeek-V4-Flash-0731-UD-IQ2_M"


def test_the_registry_holds_exactly_the_configured_model() -> None:
    document = _piconfig.models(_piconfig.Config.from_environ(ENVIRON))

    provider = document["providers"]["llamacpp"]
    assert provider["api"] == "openai-completions"
    assert provider["compat"] == {"supportsDeveloperRole": False}
    assert provider["models"] == [
        {
            "contextWindow": 262144,
            "id": "DeepSeek-V4-Flash-0731-UD-IQ2_M",
            "name": "DeepSeek V4 Flash 0731 (GB10)",
            "reasoning": True,
        }
    ]


def test_the_defaults_name_the_one_model_that_is_registered() -> None:
    """pi resolving a default the registry does not hold is a run that never starts."""
    config = _piconfig.Config.from_environ(ENVIRON)
    settings = _piconfig.settings(config)
    registry = _piconfig.models(config)["providers"]

    assert settings["defaultProvider"] in registry
    assert settings["defaultModel"] == config.model_id
    assert settings["defaultThinkingLevel"] == "high"


def test_a_run_makes_no_telemetry_call_and_prints_no_banner() -> None:
    """Not configuration: the run is non-interactive and its stderr is evidence."""
    settings = _piconfig.settings(_piconfig.Config.from_environ(ENVIRON))

    assert settings["quietStartup"] is True
    assert settings["enableInstallTelemetry"] is False


# ------------------------------------------------------------------- writing


def test_write_produces_the_two_files_pi_reads(tmp_path: Path) -> None:
    config = _piconfig.Config.from_environ(ENVIRON)

    models_path, settings_path = _piconfig.write(config, tmp_path / "agent")

    assert models_path.name == "models.json"
    assert settings_path.name == "settings.json"
    assert json.loads(models_path.read_text()) == _piconfig.models(config)
    assert json.loads(settings_path.read_text()) == _piconfig.settings(config)


def test_write_replaces_what_an_earlier_run_left(tmp_path: Path) -> None:
    """A container is --rm, but a bind-mounted config directory need not be."""
    stale = tmp_path / "models.json"
    stale.write_text('{"providers": {"stale": {}}}')

    _piconfig.write(_piconfig.Config.from_environ(ENVIRON), tmp_path)

    assert "stale" not in stale.read_text()


def test_the_config_files_are_readable_by_their_owner_alone(tmp_path: Path) -> None:
    """models.json holds the model API key in clear. Only the account pi runs
    as has any business reading it, and the default umask would say otherwise."""
    models_path, settings_path = _piconfig.write(
        _piconfig.Config.from_environ(ENVIRON), tmp_path / "agent"
    )

    assert stat.S_IMODE(models_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(settings_path.stat().st_mode) == 0o600


def test_a_stale_world_readable_file_is_tightened_not_kept(tmp_path: Path) -> None:
    """O_CREAT's mode applies only to a new file; an old one keeps its own."""
    stale = tmp_path / "models.json"
    stale.write_text("{}")
    stale.chmod(0o644)

    _piconfig.write(_piconfig.Config.from_environ(ENVIRON), tmp_path)

    assert stat.S_IMODE(stale.stat().st_mode) == 0o600
