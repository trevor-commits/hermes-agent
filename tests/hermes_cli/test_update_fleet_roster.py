"""Fleet coverage accepts both native roster schemas without widening ownership."""
from pathlib import Path

import pytest

from hermes_cli import update_receipt as ur


@pytest.fixture
def profile_homes(tmp_path):
    root = tmp_path / "home"
    homes = {"default": root, "coder": root / "profiles" / "coder"}
    for home in homes.values():
        home.mkdir(parents=True, exist_ok=True)
    return homes


def descriptor(home):
    return {"home": str(home), "profile_id": str(home)}


@pytest.mark.parametrize("source", ["socket", "status"])
@pytest.mark.parametrize("schema", ["names", "homes"])
def test_fleet_preserves_verified_multiplexer_coverage(monkeypatch, profile_homes, source, schema):
    homes = profile_homes
    roster = list(homes) if schema == "names" else [descriptor(home) for home in homes.values()]
    identity = {"pid": 4242, "code_sha": "a" * 40, "code_version": "0.22.1",
                "gateway_state": "running", "served_profiles": roster}
    monkeypatch.setattr(ur, "_profile_homes", lambda: list(homes.items()))
    monkeypatch.setattr(ur, "_code_identity", lambda refresh=False: {"sha": "a" * 40})
    monkeypatch.setattr(ur, "_updater_code_root", lambda: Path(__file__).parents[2])
    monkeypatch.setattr(ur, "_gateway_code_root", lambda pid, home: Path(__file__).parents[2])
    monkeypatch.setattr(ur, "_socket_identity", lambda home: (
        (4242, identity) if source == "socket" and home == homes["default"] else None))
    monkeypatch.setattr("gateway.status.read_runtime_status", lambda path: (
        identity if source == "status" and path.parent == homes["default"] else None))
    monkeypatch.setattr("gateway.status.live_gateway_pid_for_home", lambda home: (
        4242 if home == homes["default"] else None))

    fleet = ur.collect_fleet_versions()

    assert len(fleet) == 1
    assert fleet[0]["pid"] == 4242
    assert fleet[0]["state"] == "current"
    assert fleet[0]["code_sha"] == "a" * 40
    assert fleet[0]["served_profiles"] == ["default", "coder"]


@pytest.mark.parametrize("schema", ["names", "homes"])
def test_normalizer_preserves_roster_order(profile_homes, schema):
    roster = ["coder", "default"] if schema == "names" else [
        descriptor(profile_homes[name]) for name in ["coder", "default"]]
    assert ur.normalize_served_profiles(roster, profile_homes) == ["coder", "default"]


def test_normalizer_matches_canonical_home_alias(profile_homes, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(profile_homes["coder"], target_is_directory=True)
    roster = [{"home": str(alias), "profile_id": str(profile_homes["coder"])}]
    assert ur.normalize_served_profiles(roster, profile_homes) == ["coder"]


@pytest.mark.parametrize("case", [
    "missing", "empty", "not-list", "empty-name", "unknown-name", "duplicate-name",
    "mixed", "non-entry", "empty-object", "missing-home", "missing-profile-id",
    "relative-home", "nonstring-home", "foreign-home", "inconsistent-home", "duplicate-home",
    "duplicate-alias", "ambiguous-known-home",
])
def test_normalizer_rejects_unverified_rosters(profile_homes, tmp_path, case):
    default, coder = profile_homes.values()
    valid = descriptor(default)
    cases = {
        "missing": None, "empty": [], "not-list": ("default",), "empty-name": [""],
        "unknown-name": ["unregistered"], "duplicate-name": ["default", "default"],
        "mixed": ["default", descriptor(coder)], "non-entry": [None], "empty-object": [{}],
        "missing-home": [{"profile_id": str(default)}], "missing-profile-id": [{"home": str(default)}],
        "relative-home": [{"home": "profiles/coder", "profile_id": "profiles/coder"}],
        "nonstring-home": [{"home": default, "profile_id": str(default)}],
        "foreign-home": [descriptor(tmp_path / "other" / "profiles" / "coder")],
        "inconsistent-home": [{"home": str(default), "profile_id": str(coder)}],
        "duplicate-home": [valid, dict(valid)],
        "duplicate-alias": [valid, descriptor(default / ".." / default.name)],
        "ambiguous-known-home": [valid],
    }
    if case == "ambiguous-known-home":
        profile_homes["alias"] = default
    assert ur.normalize_served_profiles(cases[case], profile_homes) is None


@pytest.mark.parametrize("source", ["socket", "status"])
def test_fleet_does_not_publish_partial_foreign_coverage(monkeypatch, profile_homes, source):
    homes = profile_homes
    identity = {"pid": 4242, "code_sha": "a" * 40, "gateway_state": "running",
                "served_profiles": [descriptor(homes["default"]), descriptor(homes["default"].parent / "foreign")]}
    monkeypatch.setattr(ur, "_profile_homes", lambda: list(homes.items()))
    monkeypatch.setattr(ur, "_code_identity", lambda refresh=False: {"sha": "a" * 40})
    monkeypatch.setattr(ur, "_socket_identity", lambda home: (
        (4242, identity) if source == "socket" and home == homes["default"] else None))
    monkeypatch.setattr("gateway.status.read_runtime_status", lambda path: (
        identity if source == "status" and path.parent == homes["default"] else None))
    monkeypatch.setattr("gateway.status.live_gateway_pid_for_home", lambda home: (
        4242 if home == homes["default"] else None))
    monkeypatch.setattr(ur, "_gateway_code_root", lambda pid, home: None)

    fleet = ur.collect_fleet_versions()

    assert len(fleet) == 1
    assert fleet[0]["pid"] == 4242
    assert fleet[0]["state"] == "current"
    assert "served_profiles" not in fleet[0]
