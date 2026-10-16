"""Opportunity stage semantics (probability, forecast category, closed/won flags)."""

from __future__ import annotations

# stage -> (probability, ForecastCategory api value, ForecastCategoryName, closed, won)
STAGE_TABLE: dict[str, tuple[int, str, str, bool, bool]] = {
    "Prospecting": (10, "Pipeline", "Pipeline", False, False),
    "Qualification": (20, "Pipeline", "Pipeline", False, False),
    "Needs Analysis": (40, "Pipeline", "Pipeline", False, False),
    "Value Proposition": (50, "BestCase", "Best Case", False, False),
    "Id. Decision Makers": (55, "BestCase", "Best Case", False, False),
    "Perception Analysis": (60, "BestCase", "Best Case", False, False),
    "Proposal/Price Quote": (65, "Commit", "Commit", False, False),
    "Negotiation/Review": (80, "Commit", "Commit", False, False),
    "Closed Won": (100, "Closed", "Closed", True, True),
    "Closed Lost": (0, "Omitted", "Omitted", True, False),
}
CATEGORY_BY_NAME = {name: api for _, api, name, _, _ in STAGE_TABLE.values()}
NAME_BY_CATEGORY = {api: name for _, api, name, _, _ in STAGE_TABLE.values()}


def stage_info(stage: str) -> dict[str, object]:
    probability, category, name, closed, won = STAGE_TABLE.get(
        stage, (10, "Pipeline", "Pipeline", False, False)
    )
    return {
        "probability": probability,
        "category": category,
        "category_name": name,
        "closed": closed,
        "won": won,
    }


def forecast_for(stage: str) -> tuple[str, str]:
    info = stage_info(stage)
    return str(info["category"]), str(info["category_name"])
