from __future__ import annotations

from datetime import date
from typing import Any


BLOCKED_SOURCE_TYPES = {"correction_announcement", "regulatory_inquiry_reply", "assurance_report", "updated_report"}


class SourceLeakageError(ValueError):
    """Raised when post-event evidence enters pre-event model inputs."""


def validate_sources(case: dict[str, Any]) -> list[str]:
    cutoff = date.fromisoformat(case["as_of_date"])
    sources = {item["source_id"]: item for item in case.get("sources", [])}
    warnings: list[str] = []

    used_ids = {
        point["source_id"]
        for year_data in case.get("financials", {}).values()
        for point in year_data.values()
    }
    for source_id in sorted(used_ids):
        if source_id not in sources:
            raise SourceLeakageError(f"输入引用了未登记来源: {source_id}")
        source = sources[source_id]
        if source.get("source_type") in BLOCKED_SOURCE_TYPES or source.get("allowed_for_model_input") is False:
            raise SourceLeakageError(f"事后来源不得进入事前输入: {source_id}")
        publication_date = source.get("publication_date")
        if not publication_date:
            warnings.append(f"来源 {source_id} 的正式发布日期待核验")
            continue
        if date.fromisoformat(publication_date) > cutoff:
            raise SourceLeakageError(f"来源晚于预警截止日: {source_id}")
    return warnings
