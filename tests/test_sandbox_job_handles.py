"""Regression for the _win_job Job Object handle leak (Windows only)."""

from __future__ import annotations

import ctypes
import gc
import os
import subprocess
import sys
import time
import unittest
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.llm.sandbox import _win_job  # noqa: E402


def _handle_count() -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL
    count = wintypes.DWORD()
    if not kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count)):
        raise OSError(ctypes.get_last_error(), "GetProcessHandleCount failed")
    return int(count.value)


@unittest.skipUnless(os.name == "nt", "Windows Job Object only")
class JobHandleLeakTests(unittest.TestCase):
    def test_repeated_job_creation_does_not_leak_handles(self):
        # Warm-up absorbs one-off loader/handle caches.
        self._call_once()
        gc.collect()
        baseline = _handle_count()

        for _ in range(8):
            self.assertTrue(self._call_once(), "Job Object creation must still succeed")
        gc.collect()
        after = _handle_count()

        delta = after - baseline
        self.assertLessEqual(delta, 2, f"job handle leak: net +{delta} over 8 calls")
        self.assertGreaterEqual(delta, -2, f"unexpected handle drop: {delta}")

    def _call_once(self) -> bool:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(0.2)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            ok = _win_job(proc.pid, 64 * 1024 * 1024)
            time.sleep(0.01)
            return bool(ok)
        finally:
            code = proc.wait(timeout=10)
            self.assertEqual(code, 0, "worker process must exit cleanly")


if __name__ == "__main__":
    unittest.main()
