# The ordinary development loop is executable evidence, built in the sandbox.
{
  pkgs,
  cli,
  project,
  src,
  checkPhase,
  wrongPython ? null,
}:
pkgs.runCommand "pip-purifier-ordinary-development" { nativeBuildInputs = [ cli ]; } ''
  export HOME=$TMPDIR
  cp -r ${src} project
  chmod -R u+w project
  cd project
  mkdir legacy-inputs
  if pip-purifier prefetch --inputs "$PWD/legacy-inputs" --venv legacy-env >legacy.log 2>&1; then
    echo 'legacy inputs without their target interpreter were accepted' >&2
    exit 1
  fi
  grep -q 'offline inputs have no target Python' legacy.log
  ${pkgs.lib.optionalString (wrongPython != null) ''
    ${wrongPython.interpreter} -m venv --without-pip .venv
    if pip-purifier prefetch --inputs ${project.inputs} >wrong.log 2>&1; then
      echo 'prefetch accepted an existing environment with another interpreter' >&2
      exit 1
    fi
    grep -q 'Recreate the virtual environment' wrong.log
    test -x .venv/bin/python
    rm -rf .venv
  ''}
  pip-purifier prefetch --inputs ${project.inputs}
  export PATH="$PWD/.venv/bin:$PATH"
  python - <<'PYTHON'
  import socket
  try:
      socket.create_connection(('1.1.1.1', 443), timeout=5)
  except OSError:
      pass
  else:
      raise AssertionError('external network is reachable: sandbox required')
  PYTHON
  ${checkPhase}
  mkdir -p $out
  cp pip-purifier.lock.json $out/
  python - <<'PYTHON' > $out/runtime.json
  import importlib.metadata as m, json, platform, sys
  print(json.dumps({'python': sys.version, 'executable': sys.executable,
                    'platform': platform.platform(),
                    'distributions': sorted((d.metadata['Name'], d.version) for d in m.distributions())}, indent=2))
  PYTHON
''
