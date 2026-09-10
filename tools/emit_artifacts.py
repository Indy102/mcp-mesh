"""Regenerate every persisted artifact from the canonical source.

schemas/output/*.json, schemas/input/*.json and schemas/fixtures/*.json are DERIVED.
They are regenerated here and verified for equality by the validator, so a persisted
file can never drift away from the module that produced it.
"""
import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import contracts as C, fixtures as F

ROOT = pathlib.Path(__file__).resolve().parent.parent


def dump(p, obj):
    p.write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n")


def main():
    out, inp, fix = (ROOT / "schemas" / d for d in ("output", "input", "fixtures"))
    for d in (out, inp, fix):
        d.mkdir(parents=True, exist_ok=True)
    written, removed = [], []
    keep_o = {f"{t}.json" for t in C.SUCCESS}
    keep_i = {f"{t}.json" for t in C.INPUTS}
    keep_f = {f"{n}.json" for n in F.FIXTURES}
    for t in sorted(C.SUCCESS):
        dump(out / f"{t}.json", C.output_schema(t)); written.append(f"output/{t}.json")
    for t in sorted(C.INPUTS):
        dump(inp / f"{t}.json", C.input_schema(t)); written.append(f"input/{t}.json")
    for n in sorted(F.FIXTURES):
        dump(fix / f"{n}.json", F.FIXTURES[n]); written.append(f"fixtures/{n}.json")
    for d, keep in ((out, keep_o), (inp, keep_i), (fix, keep_f)):
        for f in d.glob("*.json"):
            if f.name not in keep:
                f.unlink(); removed.append(f"{d.name}/{f.name}")
    print(f"regenerated={len(written)} removed_stale={len(removed)} "
          f"stale_names={removed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
