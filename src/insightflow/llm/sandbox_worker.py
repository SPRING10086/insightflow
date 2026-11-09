"""Sandbox worker: runs one validated snippet in a restricted namespace.

Only the parent (which already validated the code) may feed it; the channel is
stdin/stdout and nothing is loaded from disk.
"""

from __future__ import annotations

import json
import pickle
import sys


def _restricted_builtins() -> dict:
    return {
        "len": len,
        "min": min,
        "max": max,
        "sum": sum,
        "round": round,
        "abs": abs,
        "int": int,
        "float": float,
        "str": str,
        "sorted": sorted,
        "bool": bool,
        "True": True,
        "False": False,
        "None": None,
    }


def main() -> int:
    try:
        # Import the data stack *before* the parent applies the memory cap: the
        # cap is meant for generated code, not for importing pandas itself.
        import numpy as np
        import pandas as pd

        sys.stdout.write("READY\n")
        sys.stdout.flush()

        payload = pickle.loads(sys.stdin.buffer.read())
        code = payload["code"]
        frame = payload["frame"]
        allowed = set(payload["allowed_columns"])
        if not payload.get("skip_validation"):
            from insightflow.llm.sandbox import validate_code

            validate_code(code, allowed)

        env = {
            "__builtins__": _restricted_builtins(),
            "pd": pd,
            "np": np,
            "df": frame.copy(),
        }
        compiled = compile(code, "<llm-sandbox>", "exec")
        exec(compiled, env)  # noqa: S102 - the whole point of the sandbox
        result = env.get("result")
        if result is None:
            raise RuntimeError("code did not assign a 'result' variable")
        sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False))
        sys.stdout.flush()
        return 0
    except BaseException as exc:  # noqa: BLE001 - report and exit non-zero
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
