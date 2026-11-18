"""Admission policy for project-selected container images."""

from django.core.exceptions import ValidationError
from docker_image.digest import InvalidDigest
from docker_image.reference import InvalidReference, Reference


def validate_builder_base_image(value: str) -> None:
    """Accept the default environment or a valid image reference pinned by SHA-256."""
    if not value:
        return
    message = "Use a container image pinned by digest: repository[:tag]@sha256:<64 lowercase hex digits>."
    try:
        reference = Reference.parse_normalized_named(value)
    except (InvalidReference, InvalidDigest) as exc:
        raise ValidationError(message) from exc
    if not (reference["digest"] or "").startswith("sha256:"):
        raise ValidationError(message)
