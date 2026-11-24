"""docker/run.sh, without Docker: what it refuses before starting a container."""

import subprocess
from pathlib import Path

RUN = Path(__file__).resolve().parents[1] / "docker" / "run.sh"


def test_a_missing_env_script_refuses_instead_of_running_without_credentials(tmp_path):
    result = subprocess.run(["bash", str(RUN), "--env", str(tmp_path / "typo.sh"), "run"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=30,
                            env={"PATH": "/usr/bin:/bin"})  # no docker on PATH: it must stop before that
    assert result.returncode == 1 and result.stderr == f"env script not found: {tmp_path / 'typo.sh'}\n"

