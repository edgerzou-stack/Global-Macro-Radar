import logging
import math
from dataclasses import dataclass, replace
from datetime import date
from typing import Callable, List

import pandas as pd


logger = logging.getLogger(__name__)


class FinancialSourceError(Exception):
    pass


class FinancialDataUnavailableError(FinancialSourceError):
    """The provider responded, but no strategy-usable statement was available."""


class FinancialNormalizationError(FinancialDataUnavailableError):
    """An official statement could not be normalized without ambiguity."""

    def __init__(
        self,
        message: str,
        *,
        period_end: date = None,
        category: str = "normalization_error",
    ):
        super().__init__(message)
        self.period_end = period_end
        self.category = category


class FinancialConflictError(FinancialNormalizationError):
    """Two official representations of the same period materially disagree."""


@dataclass
class CumulativeObservation:
    fiscal_year: int
    period_code: str
    duration_months: int
    period_end: date
    filed_date: date
    revenue: float
    net_income: float
    currency: str
    source: str
    source_document: str
    reporting_frequency: str = ""
    duration_days: int = 0


@dataclass(frozen=True)
class NormalizationIssue:
    fiscal_year: int
    period_end: date
    category: str
    reason: str

    def as_dict(self) -> dict:
        return {
            "fiscal_year": self.fiscal_year,
            "period_end": self.period_end.isoformat() if self.period_end else "",
            "category": self.category,
            "reason": self.reason,
        }


@dataclass
class NormalizationResult:
    observations: List[CumulativeObservation]
    blocking_issues: List[NormalizationIssue]
    quarantined_issues: List[NormalizationIssue]
    reporting_frequency: str = ""
    currency: str = ""

    def raise_for_blocking_issues(self) -> None:
        if not self.blocking_issues:
            return
        issue = max(
            self.blocking_issues,
            key=lambda item: (item.period_end or date.min, item.fiscal_year),
        )
        error_type = (
            FinancialConflictError
            if issue.category == "native_derived_conflict"
            else FinancialNormalizationError
        )
        raise error_type(
            issue.reason,
            period_end=issue.period_end,
            category=issue.category,
        )


def _duration_kind(observation: CumulativeObservation) -> str:
    """Classify statement duration with tolerance for 52/53-week fiscal years."""
    days = observation.duration_days
    if not days:
        days = round(float(observation.duration_months) * 365.25 / 12.0)
    if 70 <= days <= 120:
        return "quarter"
    if 150 <= days <= 220:
        return "half"
    if 240 <= days <= 310:
        return "nine_month"
    if 330 <= days <= 400:
        return "annual"
    raise FinancialNormalizationError(
        f"Unsupported statement duration: {days} days for "
        f"FY{observation.fiscal_year}/{observation.period_code}",
        period_end=observation.period_end,
        category="unsupported_duration",
    )


def _quarter_code(period_code: str) -> str:
    code = str(period_code).strip().upper().replace("-", "")
    aliases = {
        "Q1": "Q1",
        "1Q": "Q1",
        "Q2": "Q2",
        "2Q": "Q2",
        "Q3": "Q3",
        "3Q": "Q3",
        "Q4": "Q4",
        "4Q": "Q4",
        # SEC Company Facts labels a native fourth-quarter duration from a
        # 10-K as fp=FY. This function is only called for quarter-length facts,
        # so the alias cannot turn a full-year duration into Q4.
        "FY": "Q4",
    }
    try:
        return aliases[code]
    except KeyError as exc:
        raise FinancialNormalizationError(
            f"Cannot identify discrete quarter from period code {period_code!r}"
        ) from exc


def _put_latest(periods: dict, key: str, observation: CumulativeObservation) -> None:
    existing = periods.get(key)
    if existing is None or observation.filed_date > existing.filed_date:
        periods[key] = observation


def _close_enough(native: float, derived: float) -> bool:
    tolerance = max(1e-6, 0.02 * max(abs(float(native)), abs(float(derived)), 1.0))
    return abs(float(native) - float(derived)) <= tolerance


def _verify_native(native: CumulativeObservation, derived: CumulativeObservation) -> None:
    if not (
        _close_enough(native.revenue, derived.revenue)
        and _close_enough(native.net_income, derived.net_income)
    ):
        raise FinancialConflictError(
            f"Native and derived {native.period_code} disagree for FY{native.fiscal_year}",
            period_end=native.period_end,
            category="native_derived_conflict",
        )


def _derive_period(
    *,
    fiscal_year: int,
    period_code: str,
    duration_months: int,
    later: CumulativeObservation,
    earlier: CumulativeObservation,
    frequency: str,
) -> CumulativeObservation:
    return CumulativeObservation(
        fiscal_year=fiscal_year,
        period_code=period_code,
        duration_months=duration_months,
        duration_days=0,
        period_end=later.period_end,
        filed_date=later.filed_date,
        revenue=float(later.revenue) - float(earlier.revenue),
        net_income=float(later.net_income) - float(earlier.net_income),
        currency=later.currency,
        source=later.source,
        source_document=later.source_document,
        reporting_frequency=frequency,
    )


def _normalize_fiscal_year(
    fiscal_year: int, periods: dict
) -> List[CumulativeObservation]:
    """Normalize one fiscal year so a bad historical year cannot poison all years."""
    currencies = {str(item.currency).upper() for item in periods.values()}
    period_end = max(item.period_end for item in periods.values())
    if len(currencies) != 1:
        raise FinancialNormalizationError(
            f"Multiple currency found for FY{fiscal_year}: {sorted(currencies)}",
            period_end=period_end,
            category="mixed_currency",
        )

    result = []
    keys = set(periods)
    quarterly_evidence = keys & {"Q1", "Q2", "Q3", "Q4", "9M"}

    if quarterly_evidence:
        q1 = periods.get("Q1")
        h1 = periods.get("H1")
        nine_month = periods.get("9M")
        annual = periods.get("FY")

        if q1 is not None:
            result.append(
                replace(q1, period_code="Q1", reporting_frequency="quarterly")
            )

        native_q2 = periods.get("Q2")
        derived_q2 = (
            _derive_period(
                fiscal_year=fiscal_year,
                period_code="Q2",
                duration_months=3,
                later=h1,
                earlier=q1,
                frequency="quarterly",
            )
            if h1 is not None and q1 is not None
            else None
        )
        if native_q2 is not None and derived_q2 is not None:
            _verify_native(native_q2, derived_q2)
        if native_q2 is not None:
            result.append(
                replace(native_q2, period_code="Q2", reporting_frequency="quarterly")
            )
        elif derived_q2 is not None:
            result.append(derived_q2)

        native_q3 = periods.get("Q3")
        derived_q3 = (
            _derive_period(
                fiscal_year=fiscal_year,
                period_code="Q3",
                duration_months=3,
                later=nine_month,
                earlier=h1,
                frequency="quarterly",
            )
            if nine_month is not None and h1 is not None
            else None
        )
        if native_q3 is not None and derived_q3 is not None:
            _verify_native(native_q3, derived_q3)
        if native_q3 is not None:
            result.append(
                replace(native_q3, period_code="Q3", reporting_frequency="quarterly")
            )
        elif derived_q3 is not None:
            result.append(derived_q3)

        native_q4 = periods.get("Q4")
        derived_q4 = (
            _derive_period(
                fiscal_year=fiscal_year,
                period_code="Q4",
                duration_months=3,
                later=annual,
                earlier=nine_month,
                frequency="quarterly",
            )
            if annual is not None and nine_month is not None
            else None
        )
        if native_q4 is not None and derived_q4 is not None:
            _verify_native(native_q4, derived_q4)
        if native_q4 is not None:
            result.append(
                replace(native_q4, period_code="Q4", reporting_frequency="quarterly")
            )
        elif derived_q4 is not None:
            result.append(derived_q4)
        return result

    h1 = periods.get("H1")
    annual = periods.get("FY")
    if h1 is None:
        raise FinancialNormalizationError(
            f"Insufficient periods for FY{fiscal_year}: {sorted(keys)}. "
            "Semiannual normalization requires H1 before FY can be split.",
            period_end=period_end,
            category="insufficient_periods",
        )

    result.append(replace(h1, reporting_frequency="semiannual"))
    if annual is not None:
        result.append(
            _derive_period(
                fiscal_year=fiscal_year,
                period_code="H2",
                duration_months=6,
                later=annual,
                earlier=h1,
                frequency="semiannual",
            )
        )
    return result


def _issue_from_error(
    fiscal_year: int,
    periods: dict,
    error: FinancialNormalizationError,
) -> NormalizationIssue:
    fallback_end = (
        max(item.period_end for item in periods.values()) if periods else None
    )
    return NormalizationIssue(
        fiscal_year=int(fiscal_year),
        period_end=error.period_end or fallback_end,
        category=error.category,
        reason=str(error),
    )


def normalize_cumulative_observations_detailed(
    observations: List[CumulativeObservation],
    *,
    quarterly_window_periods: int = 5,
    semiannual_window_periods: int = 3,
) -> NormalizationResult:
    """Normalize official observations and isolate issues outside the decision window.

    The growth strategy needs five quarterly periods (latest YoY plus three
    consecutive QoQ comparisons) or three semiannual periods. An error is
    blocking only when its report end overlaps that latest required window.
    Older errors remain auditable but cannot discard an otherwise current,
    internally consistent statement.
    """
    if quarterly_window_periods <= 0 or semiannual_window_periods <= 0:
        raise ValueError("Normalization decision-window sizes must be positive")
    if not observations:
        return NormalizationResult([], [], [])

    by_fiscal_year = {}
    issues = []
    for observation in sorted(
        observations, key=lambda item: (item.period_end, item.filed_date)
    ):
        fiscal_year = int(observation.fiscal_year)
        periods = by_fiscal_year.setdefault(fiscal_year, {})
        try:
            kind = _duration_kind(observation)
            if kind == "quarter":
                key = _quarter_code(observation.period_code)
            elif kind == "half":
                key = "H1"
            elif kind == "nine_month":
                key = "9M"
            else:
                key = "FY"
            _put_latest(periods, key, observation)
        except FinancialNormalizationError as error:
            issues.append(
                NormalizationIssue(
                    fiscal_year=fiscal_year,
                    period_end=error.period_end or observation.period_end,
                    category=error.category,
                    reason=str(error),
                )
            )

    result = []
    for fiscal_year in sorted(by_fiscal_year):
        periods = by_fiscal_year[fiscal_year]
        if not periods:
            continue
        try:
            result.extend(_normalize_fiscal_year(fiscal_year, periods))
        except FinancialNormalizationError as error:
            issues.append(_issue_from_error(fiscal_year, periods, error))

    ordered = sorted(result, key=lambda item: item.period_end)
    if not ordered:
        return NormalizationResult(
            observations=[],
            blocking_issues=sorted(
                issues, key=lambda item: (item.period_end or date.min, item.fiscal_year)
            ),
            quarantined_issues=[],
        )

    latest = max(ordered, key=lambda item: (item.period_end, item.filed_date))
    latest_frequency = latest.reporting_frequency
    latest_currency = str(latest.currency).upper()
    compatible = [
        item
        for item in ordered
        if item.reporting_frequency == latest_frequency
        and str(item.currency).upper() == latest_currency
    ]
    required_periods = (
        quarterly_window_periods
        if latest_frequency == "quarterly"
        else semiannual_window_periods
    )
    newest_first = sorted(
        compatible,
        key=lambda item: (item.period_end, item.filed_date),
        reverse=True,
    )
    if len(newest_first) >= required_periods:
        decision_window_start = newest_first[required_periods - 1].period_end
    else:
        decision_window_start = min(item.period_end for item in newest_first)

    blocking = [
        issue
        for issue in issues
        if issue.period_end is None or issue.period_end >= decision_window_start
    ]
    quarantined = [issue for issue in issues if issue not in blocking]
    return NormalizationResult(
        observations=compatible,
        blocking_issues=sorted(
            blocking, key=lambda item: (item.period_end or date.min, item.fiscal_year)
        ),
        quarantined_issues=sorted(
            quarantined,
            key=lambda item: (item.period_end or date.min, item.fiscal_year),
        ),
        reporting_frequency=latest_frequency,
        currency=latest_currency,
    )


def normalize_cumulative_observations(
    observations: List[CumulativeObservation],
) -> List[CumulativeObservation]:
    """Convert cumulative filings into strategy-usable discrete periods."""
    normalized = normalize_cumulative_observations_detailed(observations)
    normalized.raise_for_blocking_issues()
    return normalized.observations


def observations_to_dataframe(
    observations: List[CumulativeObservation],
    *,
    normalization_diagnostics: NormalizationResult = None,
) -> pd.DataFrame:
    if not observations:
        return pd.DataFrame()

    ordered = sorted(observations, key=lambda item: item.period_end, reverse=True)
    latest_frequency = ordered[0].reporting_frequency
    if latest_frequency not in {"quarterly", "semiannual"}:
        raise FinancialNormalizationError("Latest observation has no valid cadence")
    ordered = [
        observation
        for observation in ordered
        if observation.reporting_frequency == latest_frequency
    ]

    data = {}
    filing_dates = {}
    documents = set()
    for observation in ordered:
        timestamp = pd.Timestamp(observation.period_end)
        data[timestamp] = [float(observation.revenue), float(observation.net_income)]
        filing_dates[observation.period_end.isoformat()] = observation.filed_date.isoformat()
        if observation.source_document:
            documents.add(str(observation.source_document))

    frame = pd.DataFrame(data, index=["Total Revenue", "Net Income"])
    frame.attrs.update(
        source=ordered[0].source,
        source_documents=sorted(documents),
        reporting_frequency=latest_frequency,
        filing_dates=filing_dates,
        point_in_time_safe=True,
    )
    if normalization_diagnostics is not None:
        frame.attrs.update(
            normalization_blocking_issues=[
                issue.as_dict()
                for issue in normalization_diagnostics.blocking_issues
            ],
            normalization_quarantined_issues=[
                issue.as_dict()
                for issue in normalization_diagnostics.quarantined_issues
            ],
        )
    return frame


def parse_sec_companyfacts(payload: dict, as_of_date: date) -> List[CumulativeObservation]:
    import sec_financials

    return sec_financials.parse_sec_companyfacts(payload, as_of_date)


def parse_hkex_financial_text(
    text: str,
    ticker: str,
    filed_date: date,
    source_document: str,
    announcement_title: str = "",
) -> CumulativeObservation:
    import hkex_financials

    return hkex_financials.parse_hkex_financial_text(
        text,
        ticker,
        filed_date,
        source_document,
        announcement_title=announcement_title,
    )


def validate_statement_frame(frame: pd.DataFrame) -> None:
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        raise FinancialSourceError("empty dataframe")
    required_rows = {"Total Revenue", "Net Income"}
    if not required_rows.issubset(frame.index):
        raise FinancialSourceError("Missing Total Revenue or Net Income")
    if not frame.attrs.get("source"):
        raise FinancialSourceError("Missing source")
    if frame.attrs.get("reporting_frequency") not in {"quarterly", "semiannual"}:
        raise FinancialSourceError("Missing or invalid reporting_frequency")
    documents = frame.attrs.get("source_documents")
    if not isinstance(documents, list) or not documents or not all(
        isinstance(document, str) and document.strip() for document in documents
    ):
        raise FinancialSourceError("Missing source_documents")

    dated_columns = pd.to_datetime(pd.Index(frame.columns), errors="coerce")
    if int(dated_columns.notna().sum()) < 2:
        raise FinancialSourceError("Insufficient dated statement columns")
    for row in required_rows:
        values = pd.to_numeric(frame.loc[row], errors="coerce")
        if not any(pd.notna(value) and math.isfinite(float(value)) for value in values):
            raise FinancialSourceError(f"No finite values for {row}")


def load_free_financial_statement(
    ticker: str,
    as_of_date: date,
    primary_loader: Callable,
    yahoo_loader: Callable,
) -> pd.DataFrame:
    failures = []
    unavailable_failures = 0
    try:
        primary_frame = primary_loader(ticker, as_of_date)
        try:
            validate_statement_frame(primary_frame)
        except FinancialSourceError as exc:
            raise FinancialDataUnavailableError(str(exc)) from exc
        return primary_frame
    except FinancialConflictError:
        # An official-source contradiction must never be hidden by an
        # unofficial aggregator fallback.
        raise
    except FinancialDataUnavailableError as exc:
        unavailable_failures += 1
        failures.append(f"primary={type(exc).__name__}: {exc}")
        logger.warning("Primary data unavailable for %s: %s", ticker, exc)
    except Exception as exc:
        failures.append(f"primary={type(exc).__name__}: {exc}")
        logger.warning("Primary loader failed for %s: %s", ticker, exc)

    try:
        yahoo_frame = yahoo_loader(ticker)
        try:
            validate_statement_frame(yahoo_frame)
        except FinancialSourceError as exc:
            raise FinancialDataUnavailableError(str(exc)) from exc
        if (
            yahoo_frame.attrs.get("point_in_time_safe") is False
            and as_of_date < date.today()
        ):
            raise FinancialDataUnavailableError(
                "Yahoo fallback is not point-in-time safe for historical effective dates"
            )
        return yahoo_frame
    except FinancialDataUnavailableError as exc:
        unavailable_failures += 1
        failures.append(f"yahoo={type(exc).__name__}: {exc}")
        logger.warning("Yahoo data unavailable for %s: %s", ticker, exc)
    except Exception as exc:
        failures.append(f"yahoo={type(exc).__name__}: {exc}")
        logger.warning("Yahoo loader failed for %s: %s", ticker, exc)

    message = "all free sources failed or returned invalid data; " + "; ".join(failures)
    if unavailable_failures == 2:
        raise FinancialDataUnavailableError(message)
    raise FinancialSourceError(message)
