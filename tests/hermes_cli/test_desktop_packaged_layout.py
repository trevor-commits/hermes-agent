"""The packaged resources dir follows the bundle LAYOUT, not the host platform.

A Windows/Linux unpacked tree (``<name>/Hermes.exe`` + sibling ``resources/``) verified on
darwin previously invented ``…/Contents/Resources`` from the platform sniff and reported the
packaged app as "stale, unstamped, or incomplete" — an update receipt refused a healthy
install (#desktop-layout-sniff).
"""
from pathlib import Path

from hermes_cli import main_desktop


def test_windows_unpacked_layout_resolves_on_darwin(tmp_path, monkeypatch):
    desktop = tmp_path / "apps/desktop"
    exe = desktop / "release/win-unpacked/Hermes.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ fake")
    monkeypatch.setattr(main_desktop, "_desktop_packaged_executable", lambda _d: exe)
    resources = main_desktop._packaged_resources_dir(desktop)
    assert resources == desktop / "release/win-unpacked/resources"


def test_darwin_bundle_layout_still_resolves(tmp_path, monkeypatch):
    desktop = tmp_path / "apps/desktop"
    exe = desktop / "release/Hermes.app/Contents/MacOS/Hermes"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"fake")
    monkeypatch.setattr(main_desktop, "_desktop_packaged_executable", lambda _d: exe)
    resources = main_desktop._packaged_resources_dir(desktop)
    assert resources == desktop / "release/Hermes.app/Contents/Resources"
