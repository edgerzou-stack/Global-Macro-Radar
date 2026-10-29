"""Exact row shapes shared by the audited review importer and resume gate."""

ORDINARY_SCORE_FIELDS = frozenset({"request_id", "score_data"})
UNSCORED_PRIORITY_FIELDS = frozenset(
    {"request_id", "score_data", "priority_display", "priority_industry_review"}
)
UNSCORED_PRIORITY_SCORE_DATA = {"review_outcome": "unscored_priority"}


def is_unscored_priority(score_data):
    return score_data == UNSCORED_PRIORITY_SCORE_DATA


def validate_manual_review_row_shape(item):
    """Reject unknown fields and malformed scoreless exceptions exactly."""

    if not isinstance(item, dict):
        raise ValueError("manual review response row must be an object")
    request_id = item.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("manual review response row request_id is invalid")
    score_data = item.get("score_data")
    if not isinstance(score_data, dict):
        raise ValueError("manual review response row score_data must be an object")
    if "review_outcome" in score_data and not is_unscored_priority(score_data):
        raise ValueError("manual review response row has invalid scoreless outcome")
    if is_unscored_priority(score_data):
        if set(item) != UNSCORED_PRIORITY_FIELDS:
            raise ValueError("manual priority score row has invalid fields")
        if not isinstance(item["priority_display"], dict) or not isinstance(
            item["priority_industry_review"], dict
        ):
            raise ValueError("manual priority score row display/review must be objects")
        return True
    if set(item) != ORDINARY_SCORE_FIELDS:
        raise ValueError("manual score row has invalid fields")
    return False
