#!/usr/bin/env python3
"""Run the intake form checks in headless Chrome."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class IntakeFormTest(unittest.TestCase):
    def test_stage1_and_stage2_validation(self):
        script = ROOT / "tests" / "test-intake.mjs"
        proc = subprocess.run(
            ["node", str(script)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            self.fail(output)
        self.assertIn("intake validation ok", proc.stdout)
