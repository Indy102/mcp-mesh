"""Generation is tested SEPARATELY from verification.

Regenerating into a scratch tree must reproduce the delivered artifacts byte-for-byte.
This never writes into the bundle.
"""
import filecmp, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class GenerationRoundTrip(unittest.TestCase):
    def test_emit_reproduces_delivered_artifacts_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "bundle"
            shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(
                ".venv", ".git", "__pycache__", "docs", "data", "*.tar.gz"))
            r = subprocess.run([sys.executable, "tools/emit_artifacts.py"],
                               cwd=work, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            diffs = []
            for sub in ("output", "input", "fixtures"):
                a, b = ROOT / "schemas" / sub, work / "schemas" / sub
                names = {p.name for p in a.glob("*.json")} | {p.name for p in b.glob("*.json")}
                for n in sorted(names):
                    if not (a / n).exists() or not (b / n).exists():
                        diffs.append(f"{sub}/{n}: present in only one tree")
                    elif not filecmp.cmp(a / n, b / n, shallow=False):
                        diffs.append(f"{sub}/{n}: bytes differ")
            self.assertEqual(diffs, [], "regeneration must reproduce the delivered bytes")

    def test_emit_reports_counts_separately(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "bundle"
            shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(
                ".venv", ".git", "__pycache__", "docs", "data", "*.tar.gz"))
            (work / "schemas" / "fixtures" / "STRAY_zz.json").write_text("{}")
            r = subprocess.run([sys.executable, "tools/emit_artifacts.py"],
                               cwd=work, capture_output=True, text=True)
            self.assertIn("regenerated=", r.stdout)
            self.assertIn("removed_stale=1", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
