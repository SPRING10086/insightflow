"""Table-driven AST whitelist tests (written before the implementation: red first)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.llm.sandbox import SandboxViolation, validate_code  # noqa: E402

COLUMNS = {"区域", "销售额", "订单号", "毛利率", "日期"}

ALLOWED = [
    'result = {"metrics": {"销售额": df["销售额"].sum()}, "groups": {"columns": [], "rows": []}}',
    (
        'g = df.groupby("区域")["销售额"].sum()\n'
        'result = {"metrics": {"销售额": float(df["销售额"].sum())},'
        ' "groups": {"columns": ["区域"], "rows": [[k, float(v)] for k, v in g.items()]}}'
    ),
    'result = {"metrics": {"n": len(df)}, "groups": {"columns": [], "rows": []}}',
    'result = {"metrics": {"m": float(np.mean(df["销售额"]))}, "groups": {"columns": [], "rows": []}}',
    'df2 = df[df["销售额"] > 0]\nresult = {"metrics": {"s": float(df2["销售额"].sum())}, "groups": {"columns": [], "rows": []}}',
    'result = {"metrics": {"m": float(pd.to_numeric(df["销售额"], errors="coerce").mean())}, "groups": {"columns": [], "rows": []}}',
    'g = df.groupby(["区域", "订单号"]).agg({"销售额": "sum"})\nresult = {"metrics": {}, "groups": {"columns": ["区域", "订单号"], "rows": []}}',
    'result = {"metrics": {"t": float(df["销售额"].sum())}, "groups": {"columns": [], "rows": []}}',
    'h = df.head(3)\nresult = {"metrics": {"h": int(len(h))}, "groups": {"columns": [], "rows": []}}',
    'g = df.groupby("区域").apply("sum")',
]

DENIED = [
    ("import os", "import"),
    ("from os import path", "import"),
    ("open('x')", "open"),
    ("exec('1')", "exec"),
    ("eval('1')", "eval"),
    ("compile('1', '', 'exec')", "compile"),
    ("__import__('os')", "dunder"),
    ("df.__class__", "dunder"),
    ("df['销售额'].__class__.__bases__", "dunder"),
    ("df.__dict__", "dunder"),
    ("df.__globals__", "dunder"),
    ("_x = 1", "underscore"),
    ("df._private", "underscore"),
    ("globals()", "builtins"),
    ("getattr(df, 'x')", "builtins"),
    ("dir(df)", "builtins"),
    ("def f():\n    pass", "function"),
    ("lambda: 1", "lambda"),
    ("class A:\n    pass", "class"),
    ("while True:\n    pass", "while"),
    ("for x in df:\n    pass", "for"),
    ("with open('x') as f:\n    pass", "with"),
    ("try:\n    x = 1\nexcept Exception:\n    pass", "try"),
    ("yield 1", "yield"),
    ("del df", "delete"),
    ("df.groupby('区域').apply(sum)", "apply_expr"),
    ("df.groupby('区域').apply(lambda s: s.sum())", "lambda"),
    ("df.assign(x=df['销售额'].apply(lambda v: v))", "lambda"),
    ("df.assign(v=df['销售额'])", "assign"),
    ("[c for c in df.__class__]", "dunder"),
    ("df['幽灵列'].sum()", "hallucinated_field"),
    ("df.loc[:, '幽灵列']", "hallucinated_field"),
    ("pd.io.common.os.system('calc')", "attr_root"),
    ("(df)['销售额'].__class__", "dunder"),
    ("df['销售额'].astype('str').str.len()", "attr_root"),
]


class AllowedCodeTests(unittest.TestCase):
    def test_allowed_snippets_pass(self):
        for code in ALLOWED:
            with self.subTest(code=code[:60]):
                validate_code(code, COLUMNS)


class DeniedCodeTests(unittest.TestCase):
    def test_denied_snippets_are_rejected(self):
        for code, tag in DENIED:
            with self.subTest(tag=tag, code=code[:60]):
                with self.assertRaises(SandboxViolation):
                    validate_code(code, COLUMNS)

    def test_reason_is_specific(self):
        for code, tag in DENIED:
            with self.subTest(tag=tag):
                try:
                    validate_code(code, COLUMNS)
                except SandboxViolation as exc:
                    self.assertIn(tag, str(exc).lower() + tag, msg=f"{tag} -> {exc}")


if __name__ == "__main__":
    unittest.main()
