from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dojo.models import SEVERITY_CHOICES
from rest_framework import serializers

API_SEVERITY_VALUES = tuple(value for value, _label in SEVERITY_CHOICES)


def empty_severity_counts() -> dict[str, int]:
    return dict.fromkeys(API_SEVERITY_VALUES, 0)


_RISK_WEIGHTS: dict[str, int] = {"Critical": 10, "High": 5, "Medium": 2, "Low": 1, "Info": 0}


def compute_risk_score(severity: dict[str, int]) -> dict:
    """
    Return a dict with ``score`` (0–100 int) and ``label`` string.

    Formula: weighted sum of active severity counts, capped at 100.
    Weights: Critical=10, High=5, Medium=2, Low=1.
    Thresholds: ≥70 → critical, ≥40 → high, ≥15 → medium, else → low.
    """
    raw = sum(_RISK_WEIGHTS.get(level, 0) * count for level, count in severity.items())
    score = min(100, raw)
    if score >= 70:
        label = "critical"
    elif score >= 40:
        label = "high"
    elif score >= 15:
        label = "medium"
    else:
        label = "low"
    return {"score": score, "label": label}


class CommaSeparatedListField(serializers.ListField):

    """Accept repeated query params and comma-separated values in each entry."""

    def get_value(self, dictionary):
        if hasattr(dictionary, "getlist"):
            values = dictionary.getlist(self.field_name)
            if values:
                return values
        return super().get_value(dictionary)

    def to_internal_value(self, data):
        if isinstance(data, str):
            data = [data]
        if isinstance(data, (list, tuple)):
            flattened = []
            for raw in data:
                if isinstance(raw, str):
                    flattened.extend(part.strip() for part in raw.split(",") if part.strip())
                elif raw is not None:
                    flattened.append(raw)
            data = flattened
        return super().to_internal_value(data)


class TimezoneNameField(serializers.CharField):
    def to_internal_value(self, data):
        value = super().to_internal_value(data).strip()
        if not value:
            return ""
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            msg = "Invalid timezone."
            raise serializers.ValidationError(msg) from exc
        return value
