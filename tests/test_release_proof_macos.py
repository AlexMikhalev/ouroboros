"""macOS notarization in the release proof: receipt, evidence and notes.

v7.7.0 shipped a DMG whose app Gatekeeper rejected while every signature
verified. The macOS receipt therefore records the notarization state the final
DMG smoke observed, and a notarized claim must carry the staple and Gatekeeper
proofs; Windows Authenticode follows the same shape in test_release_proof.py.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.test_release_proof import REPO, _assemble_windows, _fixture_release, _run_cli, release_proof


@pytest.mark.parametrize("notarized", [False, True])
def test_macos_notarization_state_reaches_evidence_and_notes(tmp_path: Path, notarized: bool):
    release_dir, version_file, readme = _fixture_release(tmp_path, notarized=notarized)
    _, notes = _assemble_windows(tmp_path, release_dir, version_file, readme, signer="")
    evidence = json.loads((release_dir / "release-evidence.json").read_text(encoding="utf-8"))
    macos = next(row for row in evidence["artifacts"] if row["proofId"] == "macos-arm64")

    if notarized:
        assert macos["notarization"] == {"status": "notarized"}
        assert "Apple notarized both the app and the DMG" in notes
        assert "this DMG is not notarized" not in notes
    else:
        assert macos["notarization"] == {"status": "not_notarized"}
        assert "this DMG is not notarized" in notes and "Open Anyway" in notes
        assert "Apple notarized" not in notes


@pytest.mark.parametrize(
    ("notarized", "mutate"),
    [
        # A notarized claim needs every staple and Gatekeeper proof.
        (True, "drop-gatekeeper"),
        (True, "drop-staple"),
        # An unnotarized DMG may not claim any of them.
        (False, "claim-gatekeeper"),
        (False, "drop-state"),
        (False, "unknown-state"),
    ],
)
def test_macos_notarization_state_must_be_consistent(tmp_path: Path, notarized: bool, mutate: str):
    release_dir, version_file, readme = _fixture_release(tmp_path, notarized=notarized)
    path = release_dir / "release-smoke-macos-arm64.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if mutate == "drop-gatekeeper":
        receipt["checks"].remove("gatekeeper_app")
    elif mutate == "drop-staple":
        receipt["checks"].remove("stapled_app")
    elif mutate == "claim-gatekeeper":
        receipt["checks"].append("gatekeeper_app")
    elif mutate == "drop-state":
        del receipt["notarization"]
    else:
        receipt["notarization"] = {"status": "stapled"}
    path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(ValueError, match="consistent notarization state"):
        _assemble_windows(tmp_path, release_dir, version_file, readme, signer="")
    assert not (release_dir / "release-evidence.json").exists()


def test_macos_receipt_records_notarization_state(tmp_path: Path):
    dmg = tmp_path / "Ouroboros-6.87.5.dmg"
    dmg.write_bytes(b"dmg")
    common = ["record-smoke", "--proof-id", "macos-arm64", "--artifact", str(dmg),
              "--commit", "a" * 40, "--tag", "v6.87.5", "--check", "packaged_cli_help"]
    plain, notarized = tmp_path / "plain.json", tmp_path / "notarized.json"
    assert _run_cli(*common, "--output", str(plain)).returncode == 0
    assert json.loads(plain.read_text())["notarization"] == {"status": "not_notarized"}
    assert _run_cli(*common, "--output", str(notarized),
                    "--macos-notarization", "notarized").returncode == 0
    assert json.loads(notarized.read_text())["notarization"] == {"status": "notarized"}
    # An empty workflow output never becomes a state.
    assert _run_cli(*common, "--output", str(tmp_path / "empty.json"),
                    "--macos-notarization", "").returncode != 0


@pytest.mark.parametrize("observed", ["notarized", "not_notarized", ""])
def test_workflow_records_the_observed_macos_notarization(tmp_path: Path, observed: str):
    """Execute the YAML receipt writer for the macOS shard with each smoke outcome."""
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("workflow shell execution requires bash")
    jobs = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]
    step = next(step for step in jobs["build"]["steps"]
                if step.get("name") == "Record packaged artifact smoke")
    dmg = tmp_path / "Ouroboros-6.87.5.dmg"
    dmg.write_bytes(b"dmg")
    (tmp_path / "dist").mkdir()
    script = (step["run"]
              .replace("python scripts/release_proof.py", " ".join(map(shlex.quote, [
                  Path(sys.executable).as_posix(), (REPO / "scripts/release_proof.py").as_posix()])))
              .replace("${{ matrix.proof_id }}", "macos-arm64")
              .replace("${{ steps.release_asset.outputs.path }}", dmg.as_posix())
              .replace("${{ steps.smoke_macos.outputs.macos_notarization }}", observed))
    result = subprocess.run([bash, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
                            cwd=tmp_path, capture_output=True, text=True,
                            env=dict(os.environ, GITHUB_SHA="a" * 40, GITHUB_REF_NAME="v6.87.5"))

    if not observed:
        # A smoke that reported nothing never becomes an unnotarized release.
        assert result.returncode != 0
        return
    assert result.returncode == 0, result.stderr
    receipt = json.loads((tmp_path / "dist" / "release-smoke-macos-arm64.json").read_text())
    assert receipt["notarization"] == {"status": observed}
    claimed = release_proof.NOTARIZATION_SMOKE_CHECKS & set(receipt["checks"])
    assert claimed == (release_proof.NOTARIZATION_SMOKE_CHECKS if observed == "notarized" else set())
    assert release_proof._notarization_state(receipt, receipt["checks"]) == {"status": observed}
