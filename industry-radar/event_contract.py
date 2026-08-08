"""Shared industrial-event vocabulary for scoring and source authority."""


INDUSTRIAL_EVENT_TYPES = frozenset(
    {
        "technical_breakthrough",
        "product_launch",
        "capacity_capex",
        "supply_chain",
        "industrial_policy",
        "funding_with_use",
        "commercial_deployment",
        "mixed_industrial_market",
        "other_industrial",
    }
)
NON_INDUSTRIAL_EVENT_TYPES = frozenset({"market_only", "non_industrial"})
EVENT_TYPES = INDUSTRIAL_EVENT_TYPES | NON_INDUSTRIAL_EVENT_TYPES
