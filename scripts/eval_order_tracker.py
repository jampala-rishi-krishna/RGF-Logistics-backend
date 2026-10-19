"""Mocked CI eval by default; --real explicitly sends SYNTHETIC fixtures to configured OpenAI.

No production records are loaded. Real output uses lexical fixture checks, not a
model-quality guarantee. The mocked run scores orchestration, facts and refusals.
"""
import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", help="Opt in to configured-provider calls using synthetic fixtures only")
    args = parser.parse_args()
    if not args.real:
        result = subprocess.run([sys.executable, "-m", "pytest", "tests/order_tracker/test_ai.py", "-q", "-p", "no:cacheprovider"], cwd=ROOT)
        print("Mocked eval threshold: 100% of fixture fact/refusal checks; no provider calls.")
        return result.returncode
    import pytest
    fixture = load("tracker_fixture", ROOT / "tests/order_tracker/conftest.py")
    evaluation = load("tracker_eval", ROOT / "tests/order_tracker/test_ai.py")
    from services import order_tracker_ai as ai
    passed = 0
    results = []
    for case in evaluation.CASES:
        with pytest.MonkeyPatch.context() as patch:
            env = fixture.tracker_env.__wrapped__(patch)
            evaluation.configure(env, case, patch)
            try:
                answer = asyncio.run(ai.ask(env.user, case["question"], env.day, env.day))
                text = answer["answer"].casefold()
                ok = answer["answer"] == ai.REFUSAL if case.get("refuse") else all(t.casefold() in text for t in case.get("expected", []))
                if case.get("not_recorded"):
                    ok = ok and any(t in text for t in ("not recorded", "hindi naka-record", "hindi naitala"))
                results.append({"id": case["id"], "pass": ok, "answer": answer["answer"]})
                passed += bool(ok)
            except Exception as exc:
                results.append({"id": case["id"], "pass": False, "error_type": type(exc).__name__})
    print(json.dumps({"mode": "real / synthetic records only", "passed": passed, "total": len(results), "score": passed / len(results), "results": results}, ensure_ascii=True, indent=2))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
