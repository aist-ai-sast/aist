"""
Management command: backfill_evolution_dedup

Two-phase backfill for findings that were imported before the evolution
deduplication feature was introduced.

  Phase 1 — Annotate (--annotate)
    For each finding that has a ``sourcefile_link`` DojoMeta but no
    ``aist:lhash``, read the file through ``AISTProjectVersion.open_source_file`` (the
    reader behind the file blob endpoint) and write the line-content hash.

    Findings whose project version is no longer accessible are skipped
    and counted in the summary.

  Phase 2 — Match (--match)
    Run evolution dedup on all in-scope findings that have aist:lhash,
    marking evolved findings as duplicates of their oldest ancestor.

Both phases respect --dry-run (nothing is written to the database).
If neither --annotate nor --match is given, both phases run.

Scope flags (can be combined):
  --pipeline-id   restrict to a single pipeline
  --product-id    restrict to a single product
  --batch-size    findings processed per DB batch (default: 500)
"""
from __future__ import annotations

import logging
from collections import defaultdict

import requests
from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef
from dojo.models import DojoMeta, Finding

from aist.dedupe.custom import SUPPORTED_SCAN_TYPES
from aist.dedupe.evolution import AIST_LHASH_META_NAME, line_fingerprint, run_evolution_dedup
from aist.integrations import egress
from aist.integrations.scm_errors import ScmFetchError
from aist.link_builder import SOURCEFILE_LINK_META_NAME, LinkBuilder
from aist.models import AISTPipeline, AISTProjectVersion
from aist.models.projects import SourceFileNotFoundError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Backfill aist:lhash content fingerprints and/or run evolution dedup "
        "on historical findings."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Report what would change without writing to the database.",
        )
        parser.add_argument(
            "--annotate",
            action="store_true",
            default=False,
            help="Fetch file content via sourcefile_link and store aist:lhash.",
        )
        parser.add_argument(
            "--match",
            action="store_true",
            default=False,
            help="Run evolution dedup on findings that already have aist:lhash.",
        )
        parser.add_argument(
            "--pipeline-id",
            type=str,
            default=None,
            metavar="ID",
            help="Restrict to a single pipeline.",
        )
        parser.add_argument(
            "--product-id",
            type=int,
            default=None,
            metavar="N",
            help="Restrict to a single product.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            metavar="N",
            help="Number of findings per processing batch (default: 500).",
        )

    # ------------------------------------------------------------------ #
    # Entry point                                                          #
    # ------------------------------------------------------------------ #

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        do_annotate: bool = options["annotate"]
        do_match: bool = options["match"]
        pipeline_id: str | None = options["pipeline_id"]
        product_id: int | None = options["product_id"]
        batch_size: int = options["batch_size"]

        # Default: run both phases when neither flag is given.
        if not do_annotate and not do_match:
            do_annotate = do_match = True

        mode_label = "dry-run" if dry_run else "apply"
        self._out(f"backfill_evolution_dedup: mode={mode_label}")

        if do_annotate:
            self._run_annotate_phase(
                pipeline_id=pipeline_id,
                product_id=product_id,
                batch_size=batch_size,
                dry_run=dry_run,
            )

        if do_match:
            self._run_match_phase(
                pipeline_id=pipeline_id,
                product_id=product_id,
                dry_run=dry_run,
            )

    # ------------------------------------------------------------------ #
    # Phase 1 — Annotate                                                  #
    # ------------------------------------------------------------------ #

    def _run_annotate_phase(
        self,
        *,
        pipeline_id: str | None,
        product_id: int | None,
        batch_size: int,
        dry_run: bool,
    ) -> None:
        self._out("annotate: starting")

        test_ids = self._test_ids_in_scope(pipeline_id=pipeline_id, product_id=product_id)
        if not test_ids:
            self._out("annotate: no tests in scope, skipping")
            return

        # Collect (finding_id, line) for findings that have sourcefile_link but no lhash.
        has_lhash = DojoMeta.objects.filter(finding_id=OuterRef("pk"), name=AIST_LHASH_META_NAME)
        findings_qs = (
            Finding.objects
            .filter(
                test_id__in=test_ids,
                test__test_type__name__in=SUPPORTED_SCAN_TYPES,
            )
            .exclude(Exists(has_lhash))
            .only("id", "line")
            .order_by("id")
        )

        finding_ids = list(findings_qs.values_list("id", flat=True))
        if not finding_ids:
            self._out("annotate: all findings already annotated")
            return

        # Fetch sourcefile_link values for these findings in one query.
        link_by_finding: dict[int, str] = dict(
            DojoMeta.objects
            .filter(finding_id__in=finding_ids, name=SOURCEFILE_LINK_META_NAME)
            .values_list("finding_id", "value"),
        )
        # Also fetch lines.
        line_by_finding: dict[int, int] = dict(
            Finding.objects
            .filter(id__in=finding_ids)
            .values_list("id", "line"),
        )

        skipped_no_link = sum(1 for fid in finding_ids if fid not in link_by_finding)

        # Group by (project_version_id, subpath) to fetch each file exactly once.
        # key → list of (finding_id, line)
        groups: dict[tuple[int, str], list[tuple[int, int]]] = defaultdict(list)
        skipped_parse_error = 0
        for fid in finding_ids:
            link = link_by_finding.get(fid)
            if not link:
                continue
            line = line_by_finding.get(fid)
            if not line:
                continue
            parsed = LinkBuilder.parse(link)
            if parsed is None:
                skipped_parse_error += 1
                continue
            groups[parsed].append((fid, line))

        annotated = 0
        skipped_fetch = 0
        skipped_blank = 0

        for (pv_id, subpath), members in groups.items():
            lines = _fetch_file_lines(pv_id, subpath)
            if lines is None:
                skipped_fetch += len(members)
                continue

            to_create = []
            for fid, line in members:
                fingerprint = line_fingerprint(lines, line)
                if fingerprint is None:
                    skipped_blank += 1
                    continue
                to_create.append(DojoMeta(name=AIST_LHASH_META_NAME, value=fingerprint, finding_id=fid))

            if to_create and not dry_run:
                DojoMeta.objects.bulk_create(to_create, ignore_conflicts=True)
            annotated += len(to_create)

        self._out(
            f"annotate: done  "
            f"annotated={annotated} "
            f"skipped_no_link={skipped_no_link} "
            f"skipped_parse_error={skipped_parse_error} "
            f"skipped_fetch_error={skipped_fetch} "
            f"skipped_blank={skipped_blank}",
        )

    # ------------------------------------------------------------------ #
    # Phase 2 — Match                                                     #
    # ------------------------------------------------------------------ #

    def _run_match_phase(
        self,
        *,
        pipeline_id: str | None,
        product_id: int | None,
        dry_run: bool,
    ) -> None:
        self._out("match: starting")

        pipelines = self._pipelines_in_scope(pipeline_id=pipeline_id, product_id=product_id)
        total_matched = 0

        for pipeline in pipelines:
            test_ids = list(pipeline.tests.values_list("id", flat=True))
            if not test_ids:
                continue
            matched = run_evolution_dedup(
                pipeline_id=pipeline.id,
                test_ids=test_ids,
                logger=logger,
                dry_run=dry_run,
            )
            total_matched += matched
            self._out(f"  pipeline={pipeline.id}: matched={matched}")

        self._out(f"match: done  total_matched={total_matched}")

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _test_ids_in_scope(
        self,
        *,
        pipeline_id: str | None,
        product_id: int | None,
    ) -> list[int]:
        pipelines = self._pipelines_in_scope(
            pipeline_id=pipeline_id,
            product_id=product_id,
        )
        ids: set[int] = set()
        for p in pipelines:
            ids.update(p.tests.values_list("id", flat=True))
        return list(ids)

    def _pipelines_in_scope(
        self,
        *,
        pipeline_id: str | None,
        product_id: int | None,
    ) -> list[AISTPipeline]:
        qs = (
            AISTPipeline.objects
            .prefetch_related("tests")
            .order_by("created")
        )
        if pipeline_id:
            qs = qs.filter(id=pipeline_id)
        if product_id:
            qs = qs.filter(project__product_id=product_id)
        return list(qs)

    def _out(self, msg: str) -> None:
        self.stdout.write(msg)


# ------------------------------------------------------------------ #
# Module-level helpers (testable in isolation)                        #
# ------------------------------------------------------------------ #

def _fetch_file_lines(project_version_id: int, subpath: str) -> list[str] | None:
    """Return the lines of ``subpath`` in the given project version, or None when it cannot be read."""
    pv = (
        AISTProjectVersion.objects
        .select_related("project__repository")
        .filter(id=project_version_id)
        .first()
    )
    if pv is None:
        return None
    try:
        with pv.open_source_file(subpath, proxy_url=egress.proxy_url_for_project_version(pv)) as source:
            data = source.read()
    except (SourceFileNotFoundError, ScmFetchError, requests.RequestException, OSError):
        return None
    return data.decode("utf-8", errors="replace").splitlines()
