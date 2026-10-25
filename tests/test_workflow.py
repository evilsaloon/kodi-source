"""Exercise the actual workflow shell block with the host's system Bash."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class WorkflowShellTests(unittest.TestCase):
    def run_prepare_step(self, force):
        # BaseLoader preserves YAML's `on` key instead of interpreting it as a
        # YAML 1.1 boolean. Run the committed block, not a duplicated command.
        workflow = yaml.load(
            (ROOT / ".github/workflows/update.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        self.assertIn("workflow_dispatch", workflow["on"])
        step = next(
            item for item in workflow["jobs"]["update"]["steps"]
            if item["name"] == "Check the stable release and prepare an IPA"
        )
        self.assertEqual(step["shell"], "bash")

        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            bin_path = temporary / "bin"
            bin_path.mkdir()
            capture = temporary / "python-args"
            python = bin_path / "python"
            python.write_text(
                '#!/bin/bash\nset -eu\nprintf \'%s\\0\' "$@" > "$CAPTURE_ARGS"\n'
            )
            python.chmod(0o755)
            environment = os.environ.copy()
            environment.update({
                "PATH": str(bin_path) + os.pathsep + environment.get("PATH", ""),
                "CAPTURE_ARGS": str(capture),
                "GITHUB_REPOSITORY": "example-owner/kodi-source",
                "FORCE_REBUILD": "true" if force else "false",
            })
            result = subprocess.run(
                ["/bin/bash", "-c", step["run"]],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = capture.read_bytes().split(b"\0")
            self.assertEqual(arguments.pop(), b"")
            arguments = [argument.decode() for argument in arguments]

        expected = [
            "scripts/update.py",
            "--repository", "example-owner/kodi-source",
            "--output-dir", "build",
            "--existing-source", "source.json",
            "--state", "state.json",
        ]
        if force:
            expected.append("--force")
        self.assertEqual(arguments, expected)

    def test_prepare_without_force_under_system_bash(self):
        self.run_prepare_step(force=False)

    def test_prepare_with_force_under_system_bash(self):
        self.run_prepare_step(force=True)


if __name__ == "__main__":
    unittest.main()
