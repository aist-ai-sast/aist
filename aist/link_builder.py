from urllib.parse import unquote, urljoin, urlparse

from django.urls import Resolver404, resolve, reverse
from dojo.models import DojoMeta

from aist.utils.pipeline_imports import _import_sast_pipeline_package
from aist.utils.urls import get_public_base_url

_import_sast_pipeline_package()

from pipeline.defect_dojo.repo_info import checkout_relative_path  # noqa: E402

SOURCEFILE_LINK_META_NAME = "sourcefile_link"
_FILE_BLOB_VIEW_NAME = "aist_api:project_version_file_blob"


class LinkBuilder:

    """Build the platform's source link of a scanned file of one project version."""

    def __init__(self, version_id: int | None, trim_path: str = ""):
        self.version_id = version_id
        self.trim_path = trim_path

    @staticmethod
    def build_project_version_file_blob(project_version_id: int, subpath: str) -> str:
        base = get_public_base_url()
        path = reverse(_FILE_BLOB_VIEW_NAME, kwargs={"project_version_id": project_version_id, "subpath": subpath})
        return urljoin(base + "/", path.lstrip("/"))

    @staticmethod
    def parse(link: str) -> tuple[int, str] | None:
        """Return (project_version_id, subpath) of a link this builder made, or None for any other URL."""
        try:
            match = resolve(unquote(urlparse(link).path))
        except Resolver404:
            return None
        if match.view_name != _FILE_BLOB_VIEW_NAME:
            return None
        return match.kwargs["project_version_id"], match.kwargs["subpath"]

    def relative_path(self, file_path: str) -> str:
        """Return `file_path` relative to the scanned checkout."""
        return checkout_relative_path(file_path, self.trim_path)

    def build(self, file_path: str) -> str | None:
        """Return the source link of `file_path`, or None without a path or a project version."""
        if not file_path or self.version_id is None:
            return None
        subpath = self.relative_path(file_path.removeprefix("file://")).lstrip("/")
        return LinkBuilder.build_project_version_file_blob(self.version_id, subpath)


def store_source_link(finding_id: int, link: str) -> None:
    """Record `link` as the finding's source link, replacing any earlier one."""
    DojoMeta.objects.update_or_create(
        finding_id=finding_id,
        name=SOURCEFILE_LINK_META_NAME,
        defaults={"value": link},
    )
