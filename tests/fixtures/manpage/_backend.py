"""A dependency-free fixture backend with plain and precompressed manpages."""
import base64
import csv
import gzip
import hashlib
import io
from pathlib import Path
import tarfile
import zipfile

ROOT = Path(__file__).parent
DIST = "manpage_probe-1.0.dist-info"


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    manpage = (ROOT / "manpage.1").read_bytes()
    files = {
        "manpage_probe.py": b"VALUE = 42\n",
        f"{DIST}/METADATA": b"Metadata-Version: 2.1\nName: manpage-probe\nVersion: 1.0\n",
        f"{DIST}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        "manpage_probe-1.0.data/data/share/man/man1/manpage-probe.1": manpage,
        "manpage_probe-1.0.data/data/share/man/man1/manpage-vendor.1.gz": gzip.compress(manpage, mtime=0),
    }
    record = io.StringIO(newline="")
    writer = csv.writer(record)
    for name, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        writer.writerow([name, "sha256=" + digest, len(content)])
    writer.writerow([f"{DIST}/RECORD", "", ""])
    files[f"{DIST}/RECORD"] = record.getvalue().encode()
    filename = "manpage_probe-1.0-py3-none-any.whl"
    with zipfile.ZipFile(Path(wheel_directory) / filename, "w") as wheel:
        for name, content in files.items():
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            wheel.writestr(info, content)
    return filename


def build_sdist(sdist_directory, config_settings=None):
    filename = "manpage_probe-1.0.tar.gz"
    with tarfile.open(Path(sdist_directory) / filename, "w:gz") as archive:
        for name in ["pyproject.toml", "_backend.py", "manpage.1"]:
            archive.add(ROOT / name, arcname="manpage_probe-1.0/" + name)
    return filename
