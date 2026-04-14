"""
Management command: backfill_evolution_dedup

Two-phase backfill for findings that were imported before the evolution
deduplication feature was introduced.

  Phase 1 — Annotate (--annotate)
    For each finding that has a ``sourcefile_link`` DojoMeta but no
    ``aist:lhash``, fetch the file content via the same logic used by
    ProjectVersionFileBlobAPI and write the line-content hash.

    Supported sources:
      - FILE_HASH versions: project_version.ensure_extracted() → local disk
      - GIT_BRANCH / GIT_HASH: SCM binding (GitHub / GitLab) → HTTP fetch

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
import re
from collections import defaultdict
from hashlib import sha256
from typing import TYPE_CHECKING

import requests
from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef
from dojo.models import DojoMeta, Finding

from aist.dedupe.custom import SUPPORTED_SCAN_TYPES
from aist.dedupe.evolution import AIST_LHASH_META_NAME, run_evolution_dedup
from aist.link_builder import LinkBuilder
from aist.models import AISTPipeline, AISTProjectVersion, VersionType

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Pattern to extract (project_version_id, subpath) from a sourcefile_link URL.
# URL example: https://host/api/projects_version/42/files/blob/src/views.py
_BLOB_URL_RE = re.compile(r"/projects_version/(\d+)/files/blob/(.+?)(?:\?.*)?$")


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
            .filter(finding_id__in=finding_ids, name="sourcefile_link")
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
            parsed = _parse_blob_url(link)
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
                try:
                    content = lines[line - 1].strip()
                except IndexError:
                    skipped_blank += 1
                    continue
                if not content:
                    skipped_blank += 1
                    continue
                h = sha256(content.encode()).hexdigest()[:16]
                to_create.append(DojoMeta(name=AIST_LHASH_META_NAME, value=h, finding_id=fid))

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

def _parse_blob_url(url: str) -> tuple[int, str] | None:
    """
    Extract (project_version_id, subpath) from a sourcefile_link URL.
    Returns None if the URL does not match the expected pattern.
    """
    m = _BLOB_URL_RE.search(url)
    if not m:
        return None
    return int(m.group(1)), m.group(2)


def _fetch_file_lines(project_version_id: int, subpath: str) -> list[str] | None:
    """
    Return the lines of ``subpath`` from the given project version, or None
    if the content cannot be retrieved.

    Mirrors the logic in ProjectVersionFileBlobAPI.get() so the same
    sources (local archive, SCM HTTP) are supported without going through HTTP.
    """
    try:
        pv = (
            AISTProjectVersion.objects
            .select_related("project__repository")
            .get(id=project_version_id)
        )
    except AISTProjectVersion.DoesNotExist:
        return None

    if pv.version_type == VersionType.FILE_HASH:
        return _lines_from_archive(pv, subpath)

    return _lines_from_scm(pv, subpath)


def _lines_from_archive(pv: AISTProjectVersion, subpath: str) -> list[str] | None:
    """Read a file from a FILE_HASH project version's extracted archive."""
    try:
        root: Path | None = pv.ensure_extracted()
    except FileNotFoundError:
        return None
    if root is None:
        return None
    safe_rel = subpath.lstrip("/")
    file_path = (root / safe_rel).resolve()
    if not file_path.exists() or not file_path.is_file():
        return None
    try:
        return file_path.read_text(errors="replace").splitlines()
    except OSError:
        return None


def _lines_from_scm(pv: AISTProjectVersion, subpath: str) -> list[str] | None:
    """Fetch a file from a git-based project version via its SCM binding."""
    if pv.version_type == VersionType.GIT_BRANCH:
        ref = (pv.last_resolved_commit or pv.version or "master").strip()
    else:
        ref = (pv.version or "master").strip()

    repo_obj = getattr(pv.project, "repository", None)
    if not repo_obj:
        return None

    binding = repo_obj.get_binding()
    if binding:
        raw_url = binding.build_raw_url(repo_obj, ref, subpath)
        headers = binding.get_auth_headers() or {}
    else:
        raw_url = LinkBuilder.build_raw_url(repo_obj.host(), ref, subpath)
        headers = {}

    try:
        resp = requests.get(raw_url, headers=headers, timeout=10, allow_redirects=True)
        if resp.status_code != 200:
            return None
        return resp.text.splitlines()
    except requests.RequestException:
        return None
