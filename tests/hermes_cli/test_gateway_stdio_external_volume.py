from pathlib import Path
from hermes_cli.gateway import _launchd_stdio_log_paths


def test_launchd_stdio_paths_move_off_external_volume(tmp_path, monkeypatch):
    external = Path("/Volumes/T7/Offload/hermes-home/logs")
    out, err = _launchd_stdio_log_paths(external, "ai.hermes.gateway")
    assert not str(out.resolve()).startswith("/Volumes/")
    assert out.name.endswith(".out.log")
    assert err.name.endswith(".error.log")


def test_launchd_stdio_paths_unchanged_on_internal_disk(tmp_path):
    internal = tmp_path / "logs"
    out, err = _launchd_stdio_log_paths(internal, "ai.hermes.gateway")
    assert out == internal / "gateway.log"
    assert err == internal / "gateway.error.log"
    assert internal.is_dir()
