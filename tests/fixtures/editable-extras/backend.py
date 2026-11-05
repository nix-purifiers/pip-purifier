"""Small stdlib-only build backend: exercise native pip without registry access."""
import base64
import csv
import gzip
import hashlib
import io
from pathlib import Path
import tarfile
import zipfile

NAME = "editable_extras-1.0.0"


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    filename = NAME + "-py3-none-any.whl"
    info = NAME + ".dist-info/"
    files = {
        "editable_extras.py": Path(__file__).with_name("editable_extras.py").read_bytes(),
        info + "METADATA": b"Metadata-Version: 2.3\nName: editable-extras\nVersion: 1.0.0\nProvides-Extra: demo\n",
        info + "WHEEL": b"Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = io.StringIO(newline="")
    writer = csv.writer(record)
    for path, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow([path, "sha256=" + digest, len(data)])
    writer.writerow([info + "RECORD", "", ""])
    files[info + "RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(Path(wheel_directory) / filename, "w") as wheel:
        for path, data in files.items():
            entry = zipfile.ZipInfo(path, (2020, 1, 1, 0, 0, 0))
            entry.external_attr = 0o644 << 16
            wheel.writestr(entry, data)
    return filename


# A valid PEP660 hook; ownership discovery must use pip's native dir_info.
build_editable = build_wheel


def build_sdist(sdist_directory, config_settings=None):
    filename = NAME + ".tar.gz"
    with open(Path(sdist_directory) / filename, "wb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for name in ["pyproject.toml", "backend.py", "editable_extras.py"]:
                    data = Path(__file__).with_name(name).read_bytes()
                    entry = tarfile.TarInfo(NAME + "/" + name)
                    entry.size = len(data)
                    entry.mode = 0o644
                    archive.addfile(entry, io.BytesIO(data))
    return filename
