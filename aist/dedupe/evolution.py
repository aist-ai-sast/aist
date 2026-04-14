from __future__ import annotations

import logging

from dojo.finding.deduplication import set_duplicate
from dojo.models import DojoMeta, Finding

logger = logging.getLogger(__name__)

AIST_EVOLUTION_TAG = "aist:evolved:auto"
AIST_LHASH_META_NAME = "aist:lhash"


def _normalize_vuln_id(vuln_id: str) -> str:
    """
    Normalize a scanner rule identifier so that format changes between scanner
    versions (dots/hyphens vs underscores) do not prevent evolution matching.

    Examples:
      "javascript.browser.security.insufficient-postmessage-origin-validation"
      → "javascript_browser_security_insufficient_postmessage_origin_validation"

    """
    return vuln_id.replace(".", "_").replace("-", "_").lower()


def run_evolution_dedup(
    *,
    pipeline_id: str,
    test_ids: list[int],
    logger: logging.Logger = logger,
    dry_run: bool = False,
) -> int:
    """
    Match non-duplicate findings from the current pipeline against historical
    findings with the same code content fingerprint (aist:lhash) and mark
    them as duplicates.

    Intended to run after enrich (aist:lhash already written to DojoMeta)
    and before AI triage dispatch.

    Uses two bulk queries instead of per-finding lookups to stay efficient
    for large pipelines (thousands of findings).

    When dry_run=True, counts matches without writing to the database.
    """
    test_id_set = set(test_ids)

    findings = list(
        Finding.objects
        .filter(test_id__in=test_id_set, duplicate=False)
        .prefetch_related("finding_meta")
        .select_related("test__engagement", "test__test_type"),
    )
    if not findings:
        logger.info("Evolution dedup: no findings to process (pipeline=%s)", pipeline_id)
        return 0

    # ------------------------------------------------------------------ #
    # Step 1 — collect (finding, lhash) pairs                             #
    # ------------------------------------------------------------------ #
    findings_with_hash: list[tuple[Finding, str]] = []
    for f in findings:
        lhash = next(
            (m.value for m in f.finding_meta.all() if m.name == AIST_LHASH_META_NAME),
            None,
        )
        if lhash:
            findings_with_hash.append((f, lhash))

    if not findings_with_hash:
        logger.info(
            "Evolution dedup: no findings with lhash (pipeline=%s)", pipeline_id,
        )
        return 0

    all_hashes = {lhash for _, lhash in findings_with_hash}
    product_ids = {f.test.engagement.product_id for f, _ in findings_with_hash}

    # ------------------------------------------------------------------ #
    # Step 2 — single bulk query for all potential ancestors               #
    # Scoped to same products, different tests, non-duplicate roots.      #
    # Mitigated findings are excluded: a new occurrence of fixed code is  #
    # a regression and must be re-triaged, not silently deduped away.     #
    # FP / OOS / Risk-Accepted findings ARE included as valid ancestors:  #
    # if the same code was already reviewed and dismissed, the new        #
    # finding should inherit that decision without re-triggering triage.  #
    # ------------------------------------------------------------------ #
    ancestor_metas = list(
        DojoMeta.objects
        .filter(
            name=AIST_LHASH_META_NAME,
            value__in=all_hashes,
            finding__test__engagement__product_id__in=product_ids,
            finding__duplicate=False,
            finding__is_mitigated=False,
        )
        .exclude(finding__test_id__in=test_id_set)
        .select_related("finding__test__engagement", "finding__test__test_type")
        .order_by("finding__created", "finding__id"),
    )

    # Build index: (product_id, file_path, vuln_id_from_tool, scan_type, lhash) → oldest ancestor.
    # scan_type is included so that different analyzers reporting the same line
    # with an empty vuln_id_from_tool do not collapse into a single match.
    # Results are ordered by (created, id), so the first entry per key is the oldest.
    ancestor_index: dict[tuple[int, str, str, str, str], Finding] = {}
    for meta in ancestor_metas:
        f = meta.finding
        if f is None:
            continue
        key = (
            f.test.engagement.product_id,
            f.file_path or "",
            _normalize_vuln_id(f.vuln_id_from_tool or ""),
            f.test.test_type.name if f.test and f.test.test_type else "",
            meta.value,
        )
        if key not in ancestor_index:
            ancestor_index[key] = f

    # ------------------------------------------------------------------ #
    # Step 3 — apply (or dry-run count) matches                           #
    # ------------------------------------------------------------------ #
    matched = 0
    for finding, lhash in findings_with_hash:
        key = (
            finding.test.engagement.product_id,
            finding.file_path or "",
            _normalize_vuln_id(finding.vuln_id_from_tool or ""),
            finding.test.test_type.name if finding.test and finding.test.test_type else "",
            lhash,
        )
        ancestor = ancestor_index.get(key)
        if ancestor is None:
            continue
        if dry_run:
            matched += 1
            continue
        try:
            set_duplicate(finding, ancestor)
            finding.tags.add(AIST_EVOLUTION_TAG)
            matched += 1
        except Exception:
            logger.exception(
                "Evolution dedup failed for finding %d (pipeline=%s)",
                finding.id,
                pipeline_id,
            )

    logger.info(
        "Evolution dedup: %d/%d findings matched (pipeline=%s, dry_run=%s)",
        matched,
        len(findings_with_hash),
        pipeline_id,
        dry_run,
    )
    return matched
