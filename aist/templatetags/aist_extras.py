import json

from django import template

register = template.Library()


@register.filter
def to_pretty_json(value):
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return "(unserializable)"


@register.filter
def versions_json(queryset):
    """Serialize a project versions queryset to a JSON array for use in data-* HTML attributes."""
    try:
        data = [{"id": str(v.id), "label": str(v)} for v in queryset]
        return json.dumps(data, ensure_ascii=True)
    except Exception:
        return "[]"
