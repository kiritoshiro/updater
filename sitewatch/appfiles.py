"""Copy one folder of a commit's GitHub archive over a web root, as the app's owner.

site-update (root) checks the archive first, then runs this through sudo -u <owner> with python3 -I,
so every write happens with the owner's rights. Like the apps' own updaters: top-level names in
keep (uploads, storage) are never touched, folders in clear are emptied first (old build assets),
everything else in the folder is written over what is there, and nothing else is deleted. The
commit is then recorded in storage/last-update.json, the file the app itself reads.
"""

from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
import zipfile

MAX_UNZIPPED = 512 * 1024 * 1024


def entries(archive, source, keep=()):
    """[(relative parts, ZipInfo)] under <root>/<source>/, refusing anything that could escape."""
    infos = archive.infolist()
    if sum(info.file_size for info in infos) > MAX_UNZIPPED:
        raise ValueError("too large when unpacked")
    roots, found = set(), []
    want = tuple(source.split("/"))
    for info in infos:
        path = PurePosixPath(info.filename)
        if ("\\" in info.filename or path.is_absolute() or not path.parts or any(p in ("", ".", "..") for p in path.parts)
                or stat.S_ISLNK(info.external_attr >> 16)):
            raise ValueError("unsafe path or link")
        roots.add(path.parts[0])
        parts = path.parts[1:]
        if parts[:len(want)] == want and len(parts) > len(want) and parts[len(want)] not in keep:
            found.append((parts[len(want):], info))
    if len(roots) != 1 or not any(not info.is_dir() for _, info in found):
        raise ValueError(f"no single top folder with files in {source}/")
    return found


def check_archive(data, source, keep=()):
    """Top-level names the folder writes (for the rollback); raises ValueError if unsafe."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return sorted({parts[0] for parts, _ in entries(archive, source, keep)})
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("not a valid ZIP file") from exc


def directory(root, parts):
    """Create or reuse each folder below root without following a symbolic link."""
    path = root
    for part in parts:
        path = path / part
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            os.mkdir(path, 0o775)
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"{path} is not a folder")
    return path


def empty(path):
    info = os.lstat(path) if os.path.lexists(path) else None
    if info is None or not stat.S_ISDIR(info.st_mode):
        return
    for child in os.listdir(path):
        child = path / child
        if os.path.isdir(child) and not os.path.islink(child):
            shutil.rmtree(child)
        else:
            os.unlink(child)


def install(zip_path, root, source, keep, clear, sha):
    root = Path(root)
    with zipfile.ZipFile(zip_path) as archive:
        found = entries(archive, source, keep)
        for name in clear:
            if name not in keep:
                empty(root / name)
        for parts, info in found:
            if info.is_dir():
                directory(root, parts)
                continue
            folder = directory(root, parts[:-1])
            fd = os.open(folder / parts[-1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o664)
            with os.fdopen(fd, "wb") as target, archive.open(info) as stream:
                shutil.copyfileobj(stream, target)
    record(root, sha)


def record(root, sha):
    """storage/last-update.json in the app's own format, replaced atomically."""
    storage = directory(Path(root), ["storage"])
    fd, name = tempfile.mkstemp(prefix=".last-update-", dir=storage)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o664)
            json.dump({"source": "commit", "ref": sha, "sha": sha,
                       "appliedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")}, stream, indent=4)
        os.replace(name, storage / "last-update.json")
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main(argv):
    names = lambda text: [n for n in text.split(",") if n]
    try:
        if len(argv) == 7 and argv[0] == "install":
            install(argv[1], argv[2], argv[3], names(argv[4]), names(argv[5]), argv[6])
        elif len(argv) == 3 and argv[0] == "record":
            record(argv[1], argv[2])
        else:
            return 2
    except (OSError, ValueError, zipfile.BadZipFile):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
