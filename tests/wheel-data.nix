# Wheel-owned data must remain locatable and hash-correct after Nix fixup.
{ pkgs, buildPipProject }:
let
  project = buildPipProject { src = ./fixtures/manpage; };
in
pkgs.runCommand "pip-purifier-wheel-data" { nativeBuildInputs = [ pkgs.python3 ]; } ''
  python -I - ${project.environment} ${project.packages.manpage-probe} <<'PY'
  import base64, csv, hashlib, pathlib, sys, zipfile
  prefix, distribution = map(pathlib.Path, sys.argv[1:])
  record, = prefix.glob('lib/python*/site-packages/manpage_probe-1.0.dist-info/RECORD')
  site = record.parent.parent
  checked = 0
  with record.open(newline="") as source:
      for relative, expected, size in csv.reader(source):
          if not expected:
              continue
          path = (site / relative).resolve()
          assert path.is_relative_to(prefix), path
          content = path.read_bytes()
          algorithm, digest = expected.split('=', 1)
          assert base64.urlsafe_b64encode(hashlib.new(algorithm, content).digest()).decode().rstrip('=') == digest, path
          assert len(content) == int(size), path
          checked += 1
  assert checked >= 5
  with zipfile.ZipFile(distribution / 'manpage_probe-1.0-py3-none-any.whl') as wheel:
      for name in ['manpage-probe.1', 'manpage-vendor.1.gz']:
          original = wheel.read('manpage_probe-1.0.data/data/share/man/man1/' + name)
          assert (prefix / 'share/man/man1' / name).read_bytes() == original
  assert not (prefix / 'share/man/man1/manpage-probe.1.gz').exists()
  PY
  touch "$out"
''
