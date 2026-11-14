# The ordinary development loop is executable evidence, built in the sandbox.
{
  pkgs,
  cli,
  project,
  src,
  checkPhase,
  wrongPython ? null,
  python ? pkgs.python3,
}:
pkgs.runCommand "pip-purifier-ordinary-development"
  {
    nativeBuildInputs = [
      cli
      python
    ];
  }
  ''
    export HOME=$TMPDIR
    cp -r ${src} project
    chmod -R u+w project
    cd project
    mkdir legacy-inputs
    if pip-purifier prefetch --inputs "$PWD/legacy-inputs" --venv legacy-env >legacy.log 2>&1; then
      echo 'incomplete offline inputs were accepted' >&2
      exit 1
    fi
    grep -q 'offline inputs are incomplete' legacy.log || { cat legacy.log >&2; exit 1; }
    ${pkgs.lib.optionalString (wrongPython != null) ''
      ${wrongPython.interpreter} -m venv --without-pip .venv
      if pip-purifier prefetch --inputs ${project.inputs} >wrong.log 2>&1; then
        echo 'prefetch accepted an existing environment with another interpreter' >&2
        exit 1
      fi
      grep -q 'target Python version/ABI' wrong.log || { cat wrong.log >&2; exit 1; }
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
