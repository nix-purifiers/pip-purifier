# Opt-in diagnostic: both selected suites are expected to fail identically.
{
  httpxSource ? builtins.fetchTree {
    type = "github";
    owner = "encode";
    repo = "httpx";
    rev = "b5addb64f0161ff6bfe94c124ef76f6a1fba5254";
  },
}:
let
  f = builtins.getFlake "github:nix-purifiers/pip-purifier/77c5742ae034a12d2a45b4212290be862e0fa772";
  pkgs = import f.inputs.nixpkgs {
    system = builtins.currentSystem;
    overlays = [ f.overlays.default ];
  };
  sdk = pkgs.python3.withPackages (ps: [ ps.pip ]);
  pure = pkgs.pip-purifier.buildPipProject {
    src = httpxSource;
    lockFile = ./pip-purifier.lock.json;
    python = pkgs.python313;
    checkPhase = "${pkgs.coreutils}/bin/timeout 180 pytest -q -m 'not network'";
  };
  versions = "import importlib.metadata as m,json; print(json.dumps(sorted((d.metadata['Name'].lower().replace('_','-'),d.version) for d in m.distributions())))";
  raw = pkgs.runCommand "httpx-python313-raw-pip-baseline" { } ''
    export HOME=$TMPDIR PIP_NO_INDEX=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
    export PIP_FIND_LINKS=${pure.inputs}/wheelhouse
    cp -r ${httpxSource} project
    chmod -R u+w project
    cd project
    ${pkgs.python313.interpreter} -m venv --without-pip .venv
    ${sdk.interpreter} -I -m pip --python .venv install -r requirements.txt
    .venv/bin/python -I -c ${pkgs.lib.escapeShellArg versions} > native-distributions.json
    ${pure.environment}/bin/python -I -c ${pkgs.lib.escapeShellArg versions} > pure-distributions.json
    diff -u pure-distributions.json native-distributions.json
    echo 'RAW PIP AND PURIFIED INSTALLED DISTRIBUTIONS MATCH'
    ${pkgs.coreutils}/bin/timeout 180 .venv/bin/pytest -q -m 'not network'
    touch $out
  '';
in
{
  inherit pure raw;
}
