"""InsightFlow — offline-first daily data insight pipeline.

The package intentionally keeps the core pipeline free of web services and
network calls.  A run reads local spreadsheets, produces deterministic
metrics and renders a self-contained HTML report with ECharts inlined.
"""

__version__ = "1.0.0"
__app_name__ = "InsightFlow"
