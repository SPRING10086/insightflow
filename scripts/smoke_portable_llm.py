"""Portable-bundle smoke for the LLM bypass (stage 5a-2).

Run with the BUNDLED interpreter, from a clean environment:

    dist\\InsightFlow\\python\\python.exe scripts\\smoke_portable_llm.py

The script refuses to trust the caller's environment: every child process is
started with PYTHONPATH/PYTHONHOME/PYTHONSTARTUP removed, a minimal PATH, and
only the bundle-internal paths. It writes logs/smoke_portable_llm.json.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIST = ROOT / "dist" / "InsightFlow"
LOCK = "requirements.lock.txt"
CHECK_PACKAGES = ("pandas", "numpy", "openpyxl", "xlrd")


class Smoke:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append({"check": name, "ok": bool(ok), "detail": detail})
        print(("PASS " if ok else "FAIL ") + name + (" — " + detail if detail else ""))
        return ok


def _clean_env(dist: Path) -> dict[str, str]:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    temp = os.environ.get("TEMP") or os.environ.get("TMP") or str(Path(tempfile.gettempdir()))
    python_dir = dist / "python"
    return {
        "SystemRoot": system_root,
        "TEMP": temp,
        "TMP": temp,
        "USERPROFILE": os.environ.get("USERPROFILE", ""),
        "PATH": os.pathsep.join([str(python_dir), str(python_dir / "Scripts"), str(Path(system_root) / "System32")]),
        "PYTHONPATH": str(dist / "app" / "src"),
        "PYTHONHOME": str(python_dir),
        "PYTHONIOENCODING": "utf-8",
        "INSIGHTFLOW_HOME": str(dist / "app"),
    }


def _lock_versions(lock_path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    if not lock_path.is_file():
        return versions
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, version = line.split("==", 1)
        versions[name.strip().lower()] = version.split(";")[0].strip()
    return versions


def _run_cli(dist: Path, project: Path, args: list[str]) -> tuple[int, str, str]:
    env = _clean_env(dist)
    proc = subprocess.run(
        [str(dist / "python" / "python.exe"), "-m", "insightflow", "run", *args, "--project-root", str(project)],
        env=env,
        cwd=str(dist / "app"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _project(root: Path) -> Path:
    project = root / "project"
    (project / "raw").mkdir(parents=True)
    (project / "config").mkdir(parents=True)
    (project / "config" / "config.json").write_text(
        json.dumps(
            {
                "source_dir": "raw",
                "output_dir": "out",
                "log_dir": "logs",
                "state_dir": "state",
                "comparison": {"enabled": True, "lookback_days": 1},
                "max_charts": 4,
            }
        ),
        encoding="utf-8",
    )
    rows = ["日期,区域,销售额", "2026-11-07,华东,100", "2026-11-07,华南,250", "2026-11-06,华东,90"]
    (project / "raw" / "smoke.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    return project


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="InsightFlow 便携包 LLM 冒烟")
    parser.add_argument("--dist", default=str(DEFAULT_DIST))
    parser.add_argument("--out", default=str(ROOT / "logs" / "smoke_portable_llm.json"))
    args = parser.parse_args(argv)

    dist = Path(args.dist).resolve()
    smoke = Smoke()
    started = time.time()

    bundled_python = dist / "python" / "python.exe"
    app_src = (dist / "app" / "src").resolve()
    smoke.check("bundled_python_exists", bundled_python.is_file(), str(bundled_python))
    if not bundled_python.is_file():
        return _finish(smoke, args.out, started)

    smoke.check(
        "running_on_bundled_interpreter",
        Path(sys.executable).resolve() == bundled_python.resolve(),
        f"sys.executable={sys.executable}",
    )
    smoke.check("python_310", sys.version_info[:2] == (3, 10), sys.version.split()[0])

    # Secrets must never ship: .env (and similar) are excluded by the build script.
    dotenv_files = [path for path in (dist / "app").rglob(".env*") if path.name != ".env.example"]
    smoke.check("no_dotenv_in_bundle", not dotenv_files, ", ".join(str(path) for path in dotenv_files[:3]) or "none")
    key_hits = []
    for path in (dist / "app").rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".json", ".txt", ".md", ".py", ".ps1", ".bat", ".vbs", ".ini", ".env"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "sk-" in text and len(text) < 1_000_000:
            for chunk in text.split():
                if chunk.startswith("sk-") and len(chunk) > 20:
                    key_hits.append(str(path))
                    break
    smoke.check("no_api_key_in_bundle", not key_hits, ", ".join(sorted(set(key_hits))[:3]) or "none")

    # The imported package must come from the bundle, never from the checkout.
    try:
        sys.path.insert(0, str(app_src))
        import insightflow  # noqa: PLC0415

        package_path = Path(insightflow.__file__).resolve()
        smoke.check("package_from_bundle", str(package_path).startswith(str(app_src)), str(package_path))
        checkout_src = (ROOT / "src").resolve()
        smoke.check(
            "no_checkout_src_on_path",
            all(not str(Path(entry).resolve()).startswith(str(checkout_src)) for entry in sys.path if entry),
            "sys.path clean",
        )
    except Exception as exc:  # noqa: BLE001
        smoke.check("package_from_bundle", False, f"{type(exc).__name__}: {exc}")
        return _finish(smoke, args.out, started)

    # Dependency versions must match requirements.lock.txt bundled with the app.
    lock_path = dist / "app" / LOCK
    locked = _lock_versions(lock_path)
    smoke.check("lock_file_present", lock_path.is_file(), str(lock_path))
    mismatches = []
    for name in CHECK_PACKAGES:
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            mismatches.append(f"{name}=missing")
            continue
        expected = locked.get(name)
        if expected and actual != expected:
            mismatches.append(f"{name}: {actual} != {expected}")
    smoke.check("versions_match_lock", not mismatches, "; ".join(mismatches) or "ok")

    # Sandbox worker: start it, run a minimal aggregation, and prove the Job cap is available.
    try:
        import pandas as pd  # noqa: PLC0415

        from insightflow.llm.sandbox import _win_job, run_sandbox  # noqa: PLC0415

        frame = pd.DataFrame({"region": ["A", "A", "B"], "revenue": [100.0, 200.0, 300.0]})
        code = (
            "g = df.groupby('region')['revenue'].sum()\n"
            "result = {'metrics': {'total': float(df['revenue'].sum())},"
            " 'groups': {'columns': ['region'], 'rows': [[str(k), float(v)] for k, v in g.items()]}}"
        )
        data = run_sandbox(code, frame, allowed_columns={"region", "revenue"}, timeout_seconds=20, memory_mb=512)
        smoke.check("worker_runs", abs(data["metrics"]["total"] - 600.0) < 1e-9, str(data["metrics"]))
        smoke.check("worker_groups", len(data["groups"]["rows"]) == 2, str(data["groups"]["rows"]))

        probe = subprocess.Popen([str(bundled_python), "-c", "import time; time.sleep(2)"], env=_clean_env(dist))
        try:
            job_ok = _win_job(probe.pid, 256 * 1024 * 1024)
        finally:
            probe.kill()
            probe.wait(timeout=10)
        smoke.check("memory_guard_job_available", bool(job_ok), f"job={job_ok}")
    except Exception as exc:  # noqa: BLE001
        smoke.check("worker_runs", False, f"{type(exc).__name__}: {exc}")

    # Four switches without any key: each must degrade safely and still render a report.
    temp_root = Path(tempfile.mkdtemp(prefix="insightflow-smoke-"))
    try:
        flags = {
            "semantic": ["--semantic", "llm"],
            "plan": ["--plan", "llm"],
            "execute": ["--execute", "llm"],
            "narrative": ["--narrative", "llm"],
        }
        for name, flag in flags.items():
            project = _project(temp_root / name)
            code, stdout, stderr = _run_cli(dist, project, ["--date", "2026-11-07", "--force", *flag])
            day = project / "out" / "2026-11-07"
            reports = list(day.glob("*.html")) if day.is_dir() else []
            report_ok = False
            residue = True
            if reports:
                html = reports[0].read_text(encoding="utf-8")
                report_ok = "智能解读" not in html or name == "narrative"
                residue = not any(token in html for token in ("[[total.", "[[quality.", "[[kpi."))
            smoke.check(
                f"switch_{name}_degrades_safely",
                code == 0 and "降级" in stderr and bool(reports) and report_ok and residue,
                f"exit={code} reports={len(reports)} degraded={'降级' in stderr}",
            )

        # Config-driven path (no CLI flag): same config must drive the switch.
        project = _project(temp_root / "config")
        cfg_path = project / "config" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["llm"] = {"enabled": True, "features": {"semantic": True}}
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        code, stdout, stderr = _run_cli(dist, project, ["--date", "2026-11-07", "--force"])
        smoke.check(
            "config_driven_switch_degrades",
            code == 0 and "降级" in stderr and (project / "out" / "2026-11-07" / "metrics.json").is_file(),
            f"exit={code} degraded={'降级' in stderr}",
        )
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    return _finish(smoke, args.out, started)


def _finish(smoke: Smoke, out_path: str, started: float) -> int:
    passed = sum(1 for row in smoke.rows if row["ok"])
    failed = [row for row in smoke.rows if not row["ok"]]
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration_seconds": round(time.time() - started, 1),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "passed": passed,
        "failed": len(failed),
        "checks": smoke.rows,
    }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{passed} passed, {len(failed)} failed · {out}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
