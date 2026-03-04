"""Regression detection: marks findings that re-appeared after being previously mitigated."""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone
from dojo.models import Finding

logger = logging.getLogger(__name__)


def detect_regressions_for_pipeline(pipeline_id: str, test_ids: list[int]) -> int:
    """
    For each active finding in *test_ids*, check whether a previously mitigated
    finding with the same hash_code exists for the same product.  If so, create
    (or update) an AISTFindingAnnotation marking it as a regression.

    Returns the number of regressions detected.
    """
    from aist.models import AISTFindingAnnotation  # local import to avoid circular

    if not test_ids:
        return 0

    # Collect product IDs covered by this pipeline's tests.
    product_ids = list(
        Finding.objects
        .filter(test_id__in=test_ids)
        .values_list("test__engagement__product_id", flat=True)
        .distinct()
    )
    if not product_ids:
        return 0

    # All mitigated hash codes for these products, excluding findings from this pipeline.
    mitigated_hashes: set[str] = set(
        Finding.objects
        .filter(
            test__engagement__product_id__in=product_ids,
            is_mitigated=True,
            hash_code__isnull=False,
        )
        .exclude(test_id__in=test_ids)
        .values_list("hash_code", flat=True)
    )
    if not mitigated_hashes:
        return 0

    # Active findings in this pipeline that share a hash with a previously mitigated one.
    regression_findings = list(
        Finding.objects
        .filter(
            test_id__in=test_ids,
            active=True,
            hash_code__in=mitigated_hashes,
        )
        .values_list("id", flat=True)
    )
    if not regression_findings:
        return 0

    now = timezone.now()
    count = 0
    with transaction.atomic():
        for finding_id in regression_findings:
            _, created = AISTFindingAnnotation.objects.update_or_create(
                finding_id=finding_id,
                defaults={"is_regression": True, "regression_detected_at": now},
            )
            count += 1

    logger.info(
        "Regression detection: %d regression(s) found (pipeline_id=%s)",
        count,
        pipeline_id,
    )
    return count
