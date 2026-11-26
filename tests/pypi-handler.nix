# SPDX-License-Identifier: MIT
#
# The PyPI handler (pkgs/pypi-handler) from the records of a lock run to a
# pip install in the sandbox. The records are those of a lock run whose
# build ran `pip install ./demo` against PyPI: demo has a setup.py and no
# pyproject.toml, so pip builds it in an isolated environment and fetches
# setuptools for it, its page, its core metadata and its wheel. The
# handler's entries, laid out as the library's tree lays them out and
# served by the library's replay server, then answer an unchanged pip
# install of demo with no network but the proxy.
{ pkgs, overlay }:
let
  inherit (pkgs) lib;
  scope = (pkgs.extend overlay).pip-purifier;
  flakeLock = builtins.fromJSON (builtins.readFile ../flake.lock);
  purifierSource = builtins.fetchTree flakeLock.nodes.${flakeLock.nodes.root.inputs.purifier}.locked;
  purifier = import purifierSource { inherit lib; };
  wheelUrl = "https://files.pythonhosted.org/packages/a3/dc/17031897dae0efacfea57dfd3a82fdd2a2aeb58e0ff71b77b87e44edc772/setuptools-80.9.0-py3-none-any.whl";
  wheel = pkgs.fetchurl {
    url = wheelUrl;
    sha256 = "062d34222ad13e0cc312a4c02d73f059e86a4acbfbdea8f8f76b28c99f306922";
  };
in
pkgs.runCommand "pip-purifier-test-pypi-handler"
  {
    nativeBuildInputs = [
      scope.pypi-handler
      (purifier.replayServer { inherit (pkgs) mitm-cache; })
      pkgs.python3
    ];
  }
  ''
    export HOME=$TMPDIR
    sri=$(python3 -c 'import base64, hashlib, sys; print("sha256-" + base64.b64encode(hashlib.sha256(open(sys.argv[1], "rb").read()).digest()).decode())' ${wheel})
    cat > records.jsonl <<EOF
    {"method": "GET", "url": "https://pypi.org/simple/setuptools/", "status": 200, "hash": "sha256-stalepage"}
    {"method": "GET", "url": "${wheelUrl}.metadata", "status": 200, "hash": "sha256-metadata"}
    {"method": "GET", "url": "${wheelUrl}", "status": 200, "hash": "$sri"}
    {"method": "GET", "url": "https://github.com/demo/archive/v1.tar.gz", "status": 200, "hash": "sha256-other"}
    EOF
    pypi-handler < records.jsonl > result.json

    # The handler claims the page, the core metadata and the wheel, and
    # not the archive on GitHub; it writes the page and keeps the wheel.
    python3 - ${wheel} <<'PY'
    import json, os, shutil, sys
    result = json.load(open("result.json"))
    assert not any("github.com" in u for u in result["claimed"]), result["claimed"]
    assert len(result["claimed"]) == 3, result["claimed"]
    for entry in result["entries"]:
        path = os.path.join("tree", entry["path"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if "contents" in entry:
            assert entry["path"] == "https/pypi.org/simple/setuptools/index.html", entry
            assert "#sha256=062d34222ad13e0cc312a4c02d73f059e86a4acbfbdea8f8f76b28c99f306922" in entry["contents"]
            open(path, "w").write(entry["contents"])
        else:
            assert entry["url"].endswith("setuptools-80.9.0-py3-none-any.whl"), entry
            shutil.copy(sys.argv[1], path)
    PY

    mitmCache=$PWD/tree
    mitmCacheConfigureHook
    for _ in $(seq 50); do
      python3 -c "import socket; socket.create_connection(('$MITM_CACHE_HOST', $MITM_CACHE_PORT)).close()" 2>/dev/null && break
      sleep 0.1
    done

    mkdir demo
    printf 'from setuptools import setup\nsetup(name="demo", version="1.0", py_modules=["demo"])\n' > demo/setup.py
    echo 'VALUE = 1' > demo/demo.py
    python3 -m venv venv
    venv/bin/python -m pip install ./demo
    venv/bin/python -c 'import demo; assert demo.VALUE == 1'
    touch $out
  ''
