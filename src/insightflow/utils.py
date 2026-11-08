"""Small, dependency-light helpers shared by the pipeline.

Everything here is deterministic and side-effect free except the explicit
file helpers (``atomic_write_*``).  Keeping these primitives in one place
makes the ingestion and cleaning layers easy to unit-test.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import unicodedata
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import pandas as pd

# ---------------------------------------------------------------------------
# Dates and times
# ---------------------------------------------------------------------------

_DATE_PATTERNS = (
    re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[T\s].*)?$"),
    re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日?(?:[T\s].*)?$"),
    re.compile(r"^(\d{4})(\d{2})(\d{2})$"),
    re.compile(r"^(\d{4})(\d{2})(\d{2})\d{2,6}$"),
)
_DMY_PATTERN = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})(?:[T\s].*)?$")
_TIME_PATTERN = re.compile(
    r"^(?:(\d{4})[-/.](\d{1,2})[-/.](\d{1,2}))?[T\s]*"
    r"(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?$"
)


def now_local(tz_name: str | None = None) -> datetime:
    """Return an aware local datetime, tolerating a missing tz database."""
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            # Windows without tzdata: fixed +08:00 is a sane fallback for the
            # default configuration and still keeps timestamps unambiguous.
            if tz_name in {"Asia/Shanghai", "Asia/Hong_Kong", "Asia/Singapore"}:
                from datetime import timezone

                return datetime.now(timezone(timedelta(hours=8)))
    return datetime.now().astimezone()


def parse_date_value(value: Any) -> date | None:
    """Parse the many date shapes found in exported business spreadsheets.

    Supports Excel serials, ``datetime`` objects, ISO strings, Chinese date
    strings, compact ``YYYYMMDD`` and common ``D/M/Y`` / ``M/D/Y`` forms.
    Unparseable values return ``None`` rather than silently inventing a date.

    Ambiguous values such as ``03/04/2026`` are interpreted **day-first**
    (2026-03-04), because this is the dominant convention for the Chinese /
    European exports this tool targets.  Use ISO ``YYYY-MM-DD`` when the
    source system may be US month-first.
    """
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
        # Excel's 1900 date system.  Restrict to a plausible date window so a
        # numeric business metric is never mistaken for a date.
        if 20_000 <= number <= 80_000:
            base = datetime(1899, 12, 30)
            return (base + timedelta(days=number)).date()
        if 19_000_101 <= number <= 21_001_231:
            text = str(int(number))
            return _date_from_ymd(text[:4], text[4:6], text[6:8])

    text = normalize_text(value)
    if not text:
        return None
    text = _clean_date_text(text)

    for pattern in _DATE_PATTERNS:
        match = pattern.match(text)
        if match:
            return _date_from_ymd(*match.groups()[:3])

    match = _DMY_PATTERN.match(text)
    if match:
        first, second, year = (int(part) for part in match.groups())
        # Explicit day/month evidence wins.  When both components are <=12
        # the value is ambiguous; default to day-first (DD/MM/YYYY).
        if first > 12 and second <= 12:
            return _date_from_ymd(year, second, first)
        if second > 12 and first <= 12:
            return _date_from_ymd(year, first, second)
        if first <= 12 and second <= 12:
            return _date_from_ymd(year, first, second)

    try:
        parsed = pd.to_datetime(text, errors="coerce")
        if parsed is not None and not pd.isna(parsed):
            return parsed.date()
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def parse_timestamp_value(value: Any) -> datetime | None:
    """Parse a timestamp while preserving a usable time-of-day component."""
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.to_pydatetime()
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isnan(number):
            return None
        if 20_000 <= number <= 80_000:
            base = datetime(1899, 12, 30)
            return base + timedelta(days=number)
        return None

    text = _clean_date_text(normalize_text(value))
    if not text:
        return None

    match = _TIME_PATTERN.match(text)
    if match:
        year, month, day, hour, minute, second = match.groups()
        if year:
            candidate = _safe_datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or 0))
            if candidate:
                return candidate
        # A pure time (09:30) is anchored to today only for hour extraction;
        # callers should prefer a date column when one exists.
        today = datetime.now().date()
        return _safe_datetime(today.year, today.month, today.day, int(hour), int(minute), int(second or 0))

    parsed_date = parse_date_value(text)
    if parsed_date:
        # Capture a trailing HH:MM if present.
        time_match = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
        if time_match:
            return _safe_datetime(
                parsed_date.year,
                parsed_date.month,
                parsed_date.day,
                int(time_match.group(1)),
                int(time_match.group(2)),
                int(time_match.group(3) or 0),
            )
        try:
            parsed = pd.to_datetime(text, errors="coerce")
            if parsed is not None and not pd.isna(parsed):
                return parsed.to_pydatetime()
        except (TypeError, ValueError, OverflowError):
            pass
    return None


def _clean_date_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("年", "-").replace("月", "-").replace("日", "")
    text = text.replace("时", ":").replace("分", ":").replace("秒", "")
    text = text.replace("T", " ").strip()
    return re.sub(r"\s+", " ", text)


def _date_from_ymd(year: Any, month: Any, day: Any) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except (TypeError, ValueError):
        return None


def _safe_datetime(year: int, month: int, day: int, hour: int, minute: int, second: int) -> datetime | None:
    try:
        return datetime(year, month, day, hour, minute, second)
    except (TypeError, ValueError):
        return None


def yesterday(reference: date | None = None) -> date:
    base = reference or now_local().date()
    return base - timedelta(days=1)


# ---------------------------------------------------------------------------
# Numbers and text
# ---------------------------------------------------------------------------

_MISSING_TOKENS = {"", "-", "--", "—", "–", "n/a", "na", "null", "none", "nan", "无", "暂无", "未填写"}


def parse_number(value: Any) -> float | None:
    """Parse a numeric business value with units, separators and percent.

    The parser deliberately does not coerce arbitrary identifiers.  A value is
    returned only when the remaining string is a valid number after removing
    known currency symbols and unit suffixes.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float, Decimal)):
        number = float(value)
        return None if math.isnan(number) or math.isinf(number) else number
    if isinstance(value, pd.Timestamp):
        return None

    text = normalize_text(value)
    if not text or text.lower() in _MISSING_TOKENS:
        return None

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    text = text.replace("，", ",").replace(" ", "")
    text = re.sub(r"[¥￥$€£]", "", text)
    text = re.sub(r"(人民币|rmb|cny|元|圆|块钱|块)$", "", text, flags=re.IGNORECASE)
    text = text.replace(",", "")
    text = text.replace("−", "-").replace("—", "-")

    multiplier = 1.0
    suffix_match = re.search(r"(亿|万|千|百|[kKmMwW])$", text)
    if suffix_match:
        suffix = suffix_match.group(1).lower()
        multiplier = {
            "亿": 1e8,
            "万": 1e4,
            "千": 1e3,
            "百": 1e2,
            "k": 1e3,
            "w": 1e4,
            "m": 1e6,
        }[suffix]
        text = text[: -len(suffix_match.group(1))]

    percent = False
    if text.endswith("%"):
        percent = True
        text = text[:-1]
    elif text.endswith("％"):
        percent = True
        text = text[:-1]

    text = text.strip()
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text):
        return None
    try:
        number = float(text) * multiplier
    except ValueError:
        return None
    if percent:
        number /= 100.0
    return -number if negative else number


def normalize_text(value: Any) -> str:
    """Normalize cell text without destroying meaningful Chinese content."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    if isinstance(value, (datetime, date, pd.Timestamp)):
        if isinstance(value, pd.Timestamp) and pd.isna(value):
            return ""
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value)
    text = text.replace("\ufeff", "").replace("\u200b", "").replace("\u3000", " ")
    # NFC keeps compatibility ideographs intact (important for mojibake
    # repair); full-width ASCII is normalized manually below.
    text = unicodedata.normalize("NFC", text)
    text = "".join(
        chr(ord(char) - 0xFEE0) if "\uff01" <= char <= "\uff5e" else (" " if char == "\u3000" else char)
        for char in text
    )
    return re.sub(r"\s+", " ", text).strip()


def looks_garbled(text: str) -> bool:
    """Heuristically detect the common UTF-8-as-GBK mojibake family."""
    if not text:
        return False
    if "\ufffd" in text or "锟斤拷" in text or "烫烫烫" in text:
        return True
    if any(
        marker in text
        for marker in (
            "璁㈠",
            "缂栧",
            "鏃ユ",
            "鍖哄",
            "鍝佺",
            "娓犻",
            "閿€",
            "鎴愭",
            "钀ユ",
            "瀹㈡",
            "鏁伴",
            "㈠",
            "㈡",
            "㈢",
            "璁(一)",
            "鏃(一)",
        )
    ):
        return True
    suspicious = sum(text.count(token) for token in ("Ã", "Â", "â€", "ï¿½", "ã€", "å", "æ", "ç", "è", "é"))
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    latin_supplement = sum(1 for char in text if 0x80 <= ord(char) <= 0xFF)
    return (suspicious >= 2 or latin_supplement / max(1, len(text)) > 0.25) and cjk == 0


def repair_mojibake(text: str) -> str:
    """Attempt to reverse a UTF-8/GBK decoding mistake.

    Only a candidate that measurably improves the text is returned, which keeps
    legitimate Chinese strings safe from destructive re-decoding.
    """
    if not text:
        return text
    candidates: list[str] = []
    attempts = (
        ("gbk", "utf-8"),
        ("latin1", "gbk"),
        ("latin1", "utf-8"),
        ("cp1252", "utf-8"),
    )
    for source_encoding, target_encoding in attempts:
        try:
            candidate = text.encode(source_encoding).decode(target_encoding)
        except (UnicodeEncodeError, UnicodeDecodeError, LookupError):
            continue
        if candidate and candidate != text:
            candidates.append(candidate)
    if not candidates:
        return text
    original_score = _text_quality(text)
    best = max(candidates, key=_text_quality)
    # A mojibake source can contain more (meaningless) CJK characters than
    # the repaired text, so raw CJK count must not veto an otherwise clear
    # repair.  Explicit mojibake markers are a strong enough signal here.
    if looks_garbled(text) and _text_quality(best) > 0:
        return best
    return best if _text_quality(best) > original_score + 1.0 else text


def maybe_repair_text(text: str) -> str:
    if not text:
        return text
    if looks_garbled(text) or (not _has_cjk(text) and _has_cjk(repair_mojibake(text))):
        return repair_mojibake(text)
    return text


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _text_quality(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    replacement = text.count("\ufffd")
    suspicious = sum(text.count(token) for token in ("锟", "斤", "拷", "烫", "屯", "Ã", "Â"))
    printable = sum(1 for char in text if char.isprintable())
    return cjk * 2.0 + printable * 0.05 - replacement * 8.0 - suspicious * 2.5


def safe_divide(numerator: float | int | None, denominator: float | int | None) -> float | None:
    if numerator is None or denominator is None:
        return None
    try:
        denominator = float(denominator)
        if denominator == 0 or math.isnan(denominator):
            return None
        return float(numerator) / denominator
    except (TypeError, ValueError):
        return None


def pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    change = safe_divide(current - previous, previous)
    if change is None:
        return None
    return change * 100.0


def iqr_bounds(values: Iterable[float], k: float = 1.5) -> tuple[float, float, float, float] | None:
    clean = [float(value) for value in values if value is not None and not math.isnan(float(value))]
    if len(clean) < 3:
        return None
    sorted_values = sorted(clean)
    q1 = quantile(sorted_values, 0.25)
    q3 = quantile(sorted_values, 0.75)
    iqr = q3 - q1
    return q1, q3, q1 - k * iqr, q3 + k * iqr


def quantile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        return 0.0
    position = (len(sorted_values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(sorted_values[lower])
    return float(sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower))


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def fmt_money(value: float | int | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"¥{value:,.{digits}f}"


def fmt_int(value: float | int | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}"


def fmt_number(value: float | int | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}"


def fmt_pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}%"


def fmt_delta_pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.{digits}f}%"


def short_money(value: float | int | None) -> str:
    if value is None:
        return "—"
    absolute = abs(float(value))
    if absolute >= 1e8:
        return f"{value / 1e8:.2f}亿"
    if absolute >= 1e4:
        return f"{value / 1e4:.1f}万"
    return f"{value:,.0f}"


def humanize_bytes(size: int | float) -> str:
    size = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def humanize_duration(milliseconds: float | int) -> str:
    ms = float(milliseconds)
    if ms < 1000:
        return f"{ms:.0f} ms"
    if ms < 60_000:
        return f"{ms / 1000:.2f} s"
    minutes, seconds = divmod(ms / 1000, 60)
    return f"{int(minutes)} 分 {seconds:.0f} 秒"


# ---------------------------------------------------------------------------
# Files, JSON and fingerprints
# ---------------------------------------------------------------------------


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    ensure_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, payload: Any, indent: int = 2) -> None:
    content = json.dumps(jsonable(payload), ensure_ascii=False, indent=indent, allow_nan=False)
    atomic_write_text(path, content + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return default


def file_fingerprint(paths: Iterable[Path], root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda p: str(p).lower()):
        try:
            stat = path.stat()
            relative = path.relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
        digest.update(relative.encode("utf-8", errors="replace"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def safe_relpath(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def jsonable(value: Any) -> Any:
    """Convert dataclasses, pandas/numpy scalars and paths to JSON-safe data."""
    if value is None:
        return None
    if is_dataclass(value) and not isinstance(value, type):
        return {key: jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if hasattr(value, "item"):
        try:
            return jsonable(value.item())
        except (ValueError, AttributeError):
            pass
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    return value


def unique_names(names: Sequence[str]) -> list[str]:
    """Make spreadsheet header names unique while preserving readability."""
    seen: dict[str, int] = {}
    result: list[str] = []
    for index, raw_name in enumerate(names, start=1):
        name = normalize_text(raw_name) or f"未命名列_{index}"
        count = seen.get(name, 0)
        seen[name] = count + 1
        result.append(name if count == 0 else f"{name}_{count + 1}")
    return result


def slugify(text: str, fallback: str = "item") -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    ascii_text = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    if ascii_text:
        return ascii_text[:60]
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"{fallback}-{digest}"


def clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def truncate(text: str, length: int = 80) -> str:
    return text if len(text) <= length else text[: length - 1] + "…"


def chunked(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def round_half_up(value: float, digits: int = 0) -> Decimal:
    quantizer = Decimal("1") if digits == 0 else Decimal("1." + "0" * digits)
    return Decimal(str(value)).quantize(quantizer, rounding=ROUND_HALF_UP)


def html_escape(value: Any) -> str:
    """Escape arbitrary values for text/attribute contexts."""
    import html

    return html.escape("" if value is None else str(value), quote=True)
