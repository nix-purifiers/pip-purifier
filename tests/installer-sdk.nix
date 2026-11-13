# Exercise the real installer SDK, its wheel export hook, and target isolation.
{
  pkgs,
  sdk ? pkgs.python3.withPackages (ps: [ ps.pip ]),
  runner ? "${
    pkgs.lib.fileset.toSource {
      root = ../pkgs/build-pip-project;
      fileset = pkgs.lib.fileset.unions [
        ../pkgs/build-pip-project/pip_runner.py
        ../pkgs/build-pip-project/legacy_build_constraints.py
        ../pkgs/build-pip-project/isolated_native.py
      ];
    }
  }/pip_runner.py",
}:
let
  wheel = pkgs.fetchurl {
    url = "https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl";
    sha256 = "fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70";
  };
in
pkgs.runCommand "pip-installer-sdk-isolation" { } ''
  export HOME=$TMPDIR
  unset PYTHONPATH
  ${pkgs.python3.interpreter} -m venv target
  ln -s ${wheel} pip-26.2.2-py3-none-any.whl
  export PIP_PURIFIER_EXPORT_WHEELS="$PWD/captured"
  ${sdk.interpreter} ${runner} --python "$PWD/target" \
    install --no-index --no-deps "$PWD/pip-26.2.2-py3-none-any.whl"
  cmp ${wheel} captured/pip-26.2.2-py3-none-any.whl
  mkdir -p $out
  sha256sum captured/pip-26.2.2-py3-none-any.whl > $out/capture.sha256
  ${sdk.interpreter} -I - <<'PYTHON' > $out/sdk.json
  import json, pip, sys
  print(json.dumps({'python': sys.version, 'basePrefix': sys.base_prefix,
                    'pip': pip.__version__, 'pipModule': pip.__file__}, indent=2))
  PYTHON
  target/bin/python -I - <<'PYTHON' > $out/target.json
  from importlib.metadata import distributions
  versions = [distribution.version for distribution in distributions()
              if distribution.metadata['Name'].lower() == 'pip']
  assert versions == ['26.2.2'], versions
  import json, sys
  print(json.dumps({'python': sys.version, 'basePrefix': sys.base_prefix,
                    'pipVersions': versions}, indent=2))
  PYTHON
  # The real installation hook must reject conflicting bytes with this name.
  printf 'conflicting wheel bytes' > captured/pip-26.2.2-py3-none-any.whl
  ${pkgs.python3.interpreter} -m venv conflicting-target
  if ${sdk.interpreter} ${runner} --python "$PWD/conflicting-target" \
      install --no-index --no-deps "$PWD/pip-26.2.2-py3-none-any.whl" > $out/conflict.log 2>&1; then
    echo 'conflicting wheel bytes were accepted' >&2
    exit 1
  fi
  grep -q 'conflicting native wheel bytes: pip-26.2.2-py3-none-any.whl' $out/conflict.log
''
