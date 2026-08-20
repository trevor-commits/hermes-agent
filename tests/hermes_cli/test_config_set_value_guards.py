"""set_config_value guards: wrapped-quote stripping + provider validation.

2026-08-20 incident: a caller passed JSON-encoded strings to
``hermes config set`` (``'"openai-codex"'``), which were stored VERBATIM into
config.yaml. The double-quoted provider then failed resolve_provider() at
runtime inside a Telegram source-card worker — where no fallback engages and
no retry is started. set_config_value now (a) strips one layer of symmetric
wrapping quotes from scalar string values, and (b) refuses to write a
``*.provider`` leaf whose value the canonical resolver rejects, unless
``--force``.
"""

import pytest
import yaml


@pytest.fixture()
def scratch_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n"
        "  provider: nous\n"
        "  default: z-ai/glm-5.3\n"
        "auxiliary:\n"
        "  compression:\n"
        "    provider: nous\n"
        "    model: deepseek/deepseek-v4-flash-0731\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    # The config module caches path + parsed config by mtime; force fresh state.
    import hermes_cli.config as cfg
    cfg._LOAD_CONFIG_CACHE.clear()
    return home


def _read(home):
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))


def test_wrapped_quotes_are_stripped_and_value_validates(scratch_home, capsys):
    from hermes_cli.config import set_config_value

    set_config_value("auxiliary.compression.provider", '"openai-codex"')
    data = _read(scratch_home)
    assert data["auxiliary"]["compression"]["provider"] == "openai-codex"
    assert "stripped wrapping quotes" in capsys.readouterr().out


def test_wrapped_quotes_stripped_on_model_values(scratch_home):
    from hermes_cli.config import set_config_value

    set_config_value("auxiliary.compression.model", '"gpt-5.6-luna"')
    data = _read(scratch_home)
    assert data["auxiliary"]["compression"]["model"] == "gpt-5.6-luna"


def test_unknown_provider_refused_without_write(scratch_home):
    from hermes_cli.config import set_config_value

    with pytest.raises(SystemExit) as excinfo:
        set_config_value(
            "auxiliary.compression.provider", "not-a-real-provider-xyz"
        )
    assert excinfo.value.code == 1
    data = _read(scratch_home)
    assert data["auxiliary"]["compression"]["provider"] == "nous"


def test_force_bypasses_provider_validation(scratch_home):
    from hermes_cli.config import set_config_value

    set_config_value(
        "auxiliary.compression.provider", "weird-future-provider", force=True
    )
    data = _read(scratch_home)
    assert data["auxiliary"]["compression"]["provider"] == "weird-future-provider"


def test_virtual_sentinels_allowed(scratch_home):
    from hermes_cli.config import set_config_value

    set_config_value("auxiliary.compression.provider", "moa")
    data = _read(scratch_home)
    assert data["auxiliary"]["compression"]["provider"] == "moa"
