"""Managed Python launchers are gateways; delayed restart watchers are not."""
from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from gateway import status
from hermes_cli._launchers import _launcher_script, runtime_command
from hermes_cli.venv_sync import relaunch_command


def commands(root: Path, args: list[str], home: Path | None = None) -> list[list[str]]:
    python = str(root / "python")
    launcher = _launcher_script("hermes", root, None)
    return [
        [python, "-I", "-c", launcher, *args],
        runtime_command(root, args, python=python),
        runtime_command(root, args, python=python, home=home),
        relaunch_command(Path(python), root, [str(root / "hermes_cli/main.py"), *args],
                         [python, "-m", "hermes_cli.main"], "hermes_cli.main"),
        relaunch_command(Path(python), root, [str(root / "hermes"), *args],
                         [python, str(root / "hermes")], None),
        relaunch_command(Path(python), root, ["-c", *args], [python, "-I", "-c", launcher], None),
    ]


@pytest.mark.parametrize("args,subcommand", [
    (["gateway", "run", "--replace"], "run"),
    (["--profile", "ops", "gateway", "run"], "run"),
    (["gateway", "restart"], "restart"),
    (["gateway", "status"], "status"),
    (["serve", "--port", "0"], "serve"),
])
def test_generated_launchers_keep_gateway_identity_and_scope(tmp_path, args, subcommand):
    root = tmp_path / "source with 'quotes'"
    home = tmp_path / ".hermes"
    for argv in commands(root, args, home):
        command = shlex.join(argv)
        assert status.looks_like_gateway_command_line(command) is (subcommand == "run")
        assert status.looks_like_gateway_runtime_command_line(command) is (subcommand in {"run", "restart"})
        if subcommand == "run":
            profile = "ops" if args[:1] == ["--profile"] else None
            assert status.profile_flag_value(command) == profile
            assert status._command_line_belongs_to_profile(command, home) is (profile is None)
            assert status._command_line_belongs_to_profile(command, home / "profiles" / "ops") is (profile == "ops")
            assert not status._command_line_belongs_to_profile(command, home / "profiles" / "ops-2")


def test_hermes_module_forwarder_is_recognized_but_other_modules_are_not(tmp_path):
    source = _launcher_script("hermes", tmp_path, None)
    argv = ["python", "-I", "-c", source, "--run-module", "hermes_cli.main", "gateway", "run"]
    assert status.looks_like_gateway_command_line(shlex.join(argv))
    argv[5] = "tui_gateway"
    assert not status.looks_like_gateway_command_line(shlex.join(argv))


def test_mentioning_a_launcher_or_gateway_does_not_make_inline_code_a_gateway(tmp_path):
    source = _launcher_script("hermes", tmp_path, None)
    programs = [
        f"# {source!r}\nimport time; time.sleep(60)",
        "import time; time.sleep(1)",
        source + "\nprint('later')",
        f"saved = {source!r}",
    ]
    for program in programs:
        argv = ["python", "-I", "-c", program, "99", "python", "-m", "hermes_cli.main", "gateway", "run"]
        assert not status.looks_like_gateway_command_line(shlex.join(argv))
        assert not status.looks_like_gateway_runtime_command_line(shlex.join(argv))


def test_live_process_read_preserves_the_generated_source_as_one_argument(tmp_path, monkeypatch):
    import psutil

    argv = commands(tmp_path, ["--profile", "ops", "gateway", "run"])[0]
    class Process:
        def cmdline(self):
            return argv

    monkeypatch.setattr(psutil, "Process", lambda pid: Process())
    monkeypatch.setattr(status.Path, "read_bytes", lambda self: (_ for _ in ()).throw(OSError("no proc")))
    command = status._read_process_cmdline(123)
    assert shlex.split(command) == argv
    assert status._record_matches_live_gateway_pid({"kind": "hermes-gateway"}, 123,
                                                  expected_home=tmp_path / "profiles" / "ops")


def test_explicit_home_cannot_be_claimed_by_default_or_sibling_profile(tmp_path):
    root = tmp_path / "home with 'quotes'"
    ops = root / "profiles" / "ops"
    argv = runtime_command(tmp_path, ["gateway", "run"], python="python", home=ops)
    command = shlex.join(argv)
    assert status.looks_like_gateway_command_line(command)
    assert status._command_line_belongs_to_profile(command, ops)
    assert not status._command_line_belongs_to_profile(command, root)
    assert not status._command_line_belongs_to_profile(command, root / "profiles" / "ops-2")
    # The generated source uses the explicit home only when the launch env is absent.
    assert status._command_line_belongs_to_profile(command, root, process_home=str(root))
    assert not status._command_line_belongs_to_profile(command, ops, process_home=str(root))
    assert not status._command_line_belongs_to_profile(command, root, process_home=status._UNSET)
    assert status._command_line_belongs_to_profile(command, ops, process_home="")
    flagged = shlex.join(runtime_command(tmp_path, ["--profile", "ops-2", "gateway", "run"],
                                        python="python", home=ops))
    assert not status._command_line_belongs_to_profile(flagged, ops)
    assert not status._command_line_belongs_to_profile(flagged, ops, process_home=str(ops))
    assert status._command_line_belongs_to_profile(flagged, root / "profiles" / "ops-2")


def test_live_launch_home_preserves_filesystem_aliases(tmp_path):
    root = tmp_path / "real"; root.mkdir()
    alias = tmp_path / "alias"; alias.symlink_to(root, target_is_directory=True)
    command = shlex.join(runtime_command(tmp_path, ["gateway", "run"], python="python"))
    assert status._command_line_belongs_to_profile(command, alias, process_home=str(root))


def test_psutil_serialization_preserves_windows_executable_and_python_escapes(tmp_path, monkeypatch):
    import psutil

    argv = runtime_command(tmp_path, ["gateway", "run"], python=r"C:\Program Files\Hermes\python.exe")
    class Process:
        def cmdline(self):
            return argv

    monkeypatch.setattr(psutil, "Process", lambda pid: Process())
    monkeypatch.setattr(status.Path, "read_bytes", lambda self: (_ for _ in ()).throw(OSError("no proc")))
    command = status._read_process_cmdline(123)
    assert shlex.split(command) == argv
    assert status.looks_like_gateway_command_line(command)


@pytest.mark.platforms("macos")
def test_macos_scanner_rereads_script_launcher_argv_and_rejects_mentions(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import hermes_cli.gateway as cli_gateway

    root = tmp_path / "source with \\slashes and 'quotes'"
    home = tmp_path / "profiles" / "ops"
    argv = commands(root, ["--profile", "ops", "gateway", "run"])[4]
    watcher = [argv[0], "-c", f"saved = {argv[3]!r}; import time; time.sleep(1)"]
    flattened = " ".join(argv)
    assert not status.looks_like_gateway_command_line(flattened)
    reads = []

    def read(pid):
        reads.append(pid)
        return shlex.join(argv if pid == 57431 else watcher)

    monkeypatch.setattr(cli_gateway, "_get_ancestor_pids", lambda: set())
    monkeypatch.setattr(cli_gateway, "get_hermes_home", lambda: home)
    monkeypatch.setattr(cli_gateway.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=f"57431 {flattened}\n57432 {' '.join(watcher)}\n"))
    monkeypatch.setattr(status, "_read_process_cmdline", read)
    monkeypatch.setattr(status, "_process_hermes_home", lambda pid: str(home))
    assert cli_gateway._scan_gateway_pids(set()) == [57431]
    assert reads == [57431, 57432]
