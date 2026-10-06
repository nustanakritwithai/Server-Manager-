"""Apache coexistence deploy files stay internally consistent."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "deploy" / "windows" / "Caddyfile.example"


def test_caddyfile_example_fronts_apache_and_simcore():
    text = EXAMPLE.read_text(encoding="utf-8")
    for snippet in (
        "profile shortlived",
        "reverse_proxy 127.0.0.1:8741",
        "reverse_proxy 127.0.0.1:8080",
        "reverse_proxy https://127.0.0.1:8443",
        "header_up Host {http.request.host}",
        "tls_insecure_skip_verify",
        "default_sni 157.85.96.139",
        "157-85-96-139.sslip.io",
        "renewal_window_ratio 0.5",
    ):
        assert snippet in text
    assert text.count("profile shortlived") == 1


def _run_powershell(script_name: str) -> None:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh is not installed")
    completed = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(ROOT / "deploy" / "windows" / script_name)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        sys.stdout.write(completed.stdout)
        sys.stderr.write(completed.stderr)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_powershell_coexist_unit_tests():
    _run_powershell("coexist.tests.ps1")


def test_powershell_bootstrap_unit_tests():
    _run_powershell("bootstrap.tests.ps1")
