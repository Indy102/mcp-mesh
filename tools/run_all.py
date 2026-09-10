"""Tiered runner. Each tier is reported SEPARATELY; a pass in one tier is never
evidence for another."""
import subprocess, sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
TIERS = [
    # Verification comes FIRST and writes nothing: the acceptance command must
    # validate exactly what was delivered.
    ("T1  READ-ONLY contract verification of the delivered artifacts",
     [sys.executable, "tools/validate_contracts.py"]),
    ("T1b artifact-integrity mutation tests (changed / missing / stray / tampered pin)",
     [sys.executable, "tests/test_artifact_integrity.py"]),
    ("T1c semantic contract regressions (identity, family, circular, arithmetic)",
     [sys.executable, "tests/test_contract_semantics.py"]),
    ("T2  executed domain functions (independent suite, unmodified)",
     [sys.executable, "-B", "tests/rev6_screentime_regressions.py", "tools/screentime.py"]),
    ("T3  database + collector (real DuckDB, real swap)", [sys.executable, "tests/test_storage.py"]),
    ("T4  stdio transport profile (real subprocess, raw wire frames)",
     [sys.executable, "tests/test_stdio.py"]),
    ("T5  generation round-trip: emit reproduces the delivered artifacts byte-for-byte",
     [sys.executable, "tests/test_generation_roundtrip.py"]),
    ("T6  public workflow: real repos -> mesh sync -> gateway/leaf transport -> report",
     [sys.executable, "tests/test_git_milestone.py"]),
]
NOT_RUN = [
    ("T7  source spikes S1-S5 (TCC, knowledgeC retention R, Health export, weather revision, git)",
     "NOT RUN - requires the target Mac and user grants"),
    ("T8  filesystem/platform spike S6 (APFS crash injection on the real volume)",
     "NOT RUN - T3 measures the DuckDB instance-cache behaviour, not crash/corruption"),
    ("T9  statistical calibration battery", "NOT RUN - not implemented in this bundle"),
]

def main():
    print("=" * 78); print("MCP MESH - TIERED VERIFICATION"); print("=" * 78)
    results = []
    for label, cmd in TIERS:
        print(f"\n>>> {label}\n    $ {' '.join(cmd)}")
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        out = r.stdout + r.stderr
        if "T1 CONTRACT CHECKS:" in out:
            line = [l for l in out.splitlines() if "T1 CONTRACT CHECKS:" in l][0]
            n = line.split("CHECKS:")[1].split()[0]; f = line.split("FAILURES:")[1].strip()
            summary = f"{n} checks, {f} failures"
        else:
            ran = [l for l in out.splitlines() if l.startswith("Ran ")]
            summary = (ran[-1].strip() if ran else "?") + (
                " -> OK" if r.returncode == 0 else " -> FAILED")
        results.append((label, r.returncode, summary))
        print(f"    {summary}")
    print("\n" + "=" * 78); print("TIER SUMMARY"); print("=" * 78)
    for label, rc, s in results:
        print(f"  [{'PASS' if rc == 0 else 'FAIL'}] {label}\n           {s}")
    print("\n  Tiers NOT executed by this bundle:")
    for label, why in NOT_RUN:
        print(f"  [ -- ] {label}\n           {why}")
    print("\n" + "=" * 78)
    bad = [l for l, rc, _ in results if rc != 0]
    print("ALL EXECUTED TIERS PASSED" if not bad else f"FAILING TIERS: {bad}")
    print("Source, TCC, APFS crash, and statistical gates remain UNVERIFIED. Status: DRAFT.")
    print("=" * 78)
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
