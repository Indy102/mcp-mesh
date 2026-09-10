"""T1b — the validator must DETECT artifact tampering and must never repair it.

Rev 8 regenerated packaged schemas before comparing them, so replacing
schemas/input/health_check.json with {"type":"null"} still produced 299/0. Each case
below asserts a NONZERO exit AND that the mutated bytes are unchanged afterwards.
"""
import hashlib, json, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IGNORE = shutil.ignore_patterns(".venv", ".git", "__pycache__", "docs", "data", "*.tar.gz")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class ArtifactIntegrity(unittest.TestCase):
    def _run(self, mutate):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "bundle"
            shutil.copytree(ROOT, work, ignore=IGNORE)
            target = mutate(work)
            before = sha(target) if target and Path(target).exists() else None
            r = subprocess.run([sys.executable, "tools/validate_contracts.py"],
                               cwd=work, capture_output=True, text=True, timeout=300)
            after = sha(target) if target and Path(target).exists() else None
            return r, before, after, work

    def test_changed_input_schema_is_detected_and_not_repaired(self):
        def m(w):
            p = w / "schemas" / "input" / "health_check.json"
            p.write_text('{"type":"null"}\n')
            return p
        r, before, after, _ = self._run(m)
        self.assertNotEqual(r.returncode, 0, "a corrupted artifact must fail verification")
        self.assertEqual(before, after, "the validator must NOT rewrite the artifact")
        self.assertIn("schemas/input", r.stdout)

    def test_changed_output_schema_is_detected_and_not_repaired(self):
        def m(w):
            p = w / "schemas" / "output" / "fetch.json"
            d = json.loads(p.read_text()); d["title"] = "tampered"
            p.write_text(json.dumps(d, indent=1, sort_keys=True) + "\n")
            return p
        r, before, after, _ = self._run(m)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(before, after)

    def test_changed_fixture_is_detected_and_not_repaired(self):
        def m(w):
            p = w / "schemas" / "fixtures" / "B5_source_status.json"
            d = json.loads(p.read_text())
            d["response"]["result"]["structuredContent"]["generation"] = 999
            p.write_text(json.dumps(d, indent=1, sort_keys=True) + "\n")
            return p
        r, before, after, _ = self._run(m)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(before, after)

    def test_missing_artifact_is_detected(self):
        def m(w):
            (w / "schemas" / "fixtures" / "B10_association_confirmatory.json").unlink()
            return None
        r, _, _, _ = self._run(m)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("missing", r.stdout.lower())

    def test_stray_artifact_is_detected(self):
        def m(w):
            p = w / "schemas" / "fixtures" / "ZZ_stray.json"
            p.write_text("{}\n")
            return p
        r, before, after, _ = self._run(m)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(before, after, "a stray file must not be deleted by verification")
        self.assertIn("stale", r.stdout.lower())

    def test_tampered_official_schema_is_detected_by_checksum(self):
        def m(w):
            p = w / "schemas" / "mcp-official-2026-07-28.json"
            d = json.loads(p.read_text()); d["$defs"]["__tamper__"] = {"type": "object"}
            p.write_text(json.dumps(d))
            return p
        r, before, after, _ = self._run(m)
        self.assertNotEqual(r.returncode, 0, "the pinned checksum must be verified")
        self.assertEqual(before, after)
        self.assertIn("checksum", r.stdout.lower())

    def test_clean_bundle_verifies_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "bundle"
            shutil.copytree(ROOT, work, ignore=IGNORE)
            files = sorted(p for p in work.rglob("*") if p.is_file())
            before = {str(p.relative_to(work)): sha(p) for p in files}
            r = subprocess.run([sys.executable, "tools/validate_contracts.py"],
                               cwd=work, capture_output=True, text=True, timeout=300)
            after_files = sorted(p for p in work.rglob("*")
                                 if p.is_file() and "__pycache__" not in str(p))
            after = {str(p.relative_to(work)): sha(p) for p in after_files}
            self.assertEqual(r.returncode, 0, r.stdout[-2000:])
            changed = [k for k in before if k in after and before[k] != after[k]]
            added = [k for k in after if k not in before]
            self.assertEqual(changed, [], "verification must not modify any file")
            self.assertEqual(added, [], "verification must not create any file")


if __name__ == "__main__":
    unittest.main(verbosity=2)
