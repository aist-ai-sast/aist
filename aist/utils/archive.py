from __future__ import annotations

import os
import shutil
from pathlib import Path


def _flatten_single_root_directory(root: Path) -> None:
    """If extraction produced a single top-level directory, move its contents up one level and remove it."""
    if not root.exists():
        return

    # ignore marker file when counting entries
    entries = [p for p in root.iterdir() if p.name != ".extracted.ok"]

    # only one entry and it's a directory -> flatten
    if len(entries) == 1 and entries[0].is_dir():
        inner_dir = entries[0]
        # Move children one-by-one (safer than renaming the directory itself)
        for child in inner_dir.iterdir():
            target = root / child.name
            if target.exists():
                # On name collision we overwrite existing files/dirs.
                # If you prefer strict behavior, raise instead of removing.
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            shutil.move(str(child), str(target))
        # remove now-empty directory
        try:
            inner_dir.rmdir()
        except OSError:
            # If not empty for any reason, purge it
            shutil.rmtree(inner_dir, ignore_errors=True)


def _safe_join(root: Path, target: str) -> Path:
    target = target.replace("\\", "/")
    joined = (root / target).resolve()
    allowed_prefix = str(root.resolve()) + os.sep
    if not str(joined).startswith(allowed_prefix):
        msg = "Illegal path in archive (path traversal detected)."
        raise ValueError(msg)
    return joined


def _safe_extract_zip_member(zf, member, root: Path) -> None:
    """Extract one member from a ZIP file safely, avoiding path traversal."""
    name = member.filename
    if name.endswith("/"):
        (_safe_join(root, name)).mkdir(parents=True, exist_ok=True)
        return
    out_path = _safe_join(root, name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(member) as src, Path(out_path).open("wb") as dst:
        while True:
            chunk = src.read(64 * 1024)
            if not chunk:
                break
            dst.write(chunk)


def _safe_extract_tar_member(tf, member, root: Path) -> None:
    """Extract one member from a TAR file safely, avoiding path traversal."""
    if not member.name:
        return
    if member.islnk() or member.issym() or member.ischr() or member.isblk() or member.isfifo():
        return
    out_path = _safe_join(root, member.name + ("/" if member.isdir() else ""))
    if member.isdir():
        out_path.mkdir(parents=True, exist_ok=True)
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    src = tf.extractfile(member)
    if src is None:
        return
    with src, Path(out_path).open("wb") as dst:
        while True:
            chunk = src.read(64 * 1024)
            if not chunk:
                break
            dst.write(chunk)
