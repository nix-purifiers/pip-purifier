{ pkgs, buildPipProject }:
let
  inherit (pkgs) lib;
  src = ./fixtures/nested-local-wheel;
  recorded = builtins.fromJSON (builtins.readFile ./fixtures/project/pip-purifier.lock.json);
  reader = import ../lib-overlays/default/read-lock.nix { inherit lib; };
  # A relocated test fixture, not a newly captured production lock. Keep every
  # recorded dependency/backend pin and artifact; only source paths change.
  relocated = recorded // {
    inputs = {
      "requirements.txt" = builtins.hashFile "sha256" (src + "/requirements.txt");
      "greet/pyproject.toml" = builtins.hashFile "sha256" (src + "/greet/pyproject.toml");
    };
    versions = {
      greet = "0.1.0";
    };
    outputs = recorded.outputs // {
      packages = recorded.outputs.packages // {
        greet = recorded.outputs.packages.greet // {
          path = "greet";
        };
      };
    };
  };
  project = buildPipProject {
    inherit src;
    lockData = relocated // {
      hash = reader.hashOf relocated;
    };
  };
  pip = pkgs.python3.withPackages (ps: [ ps.pip ]);
in
pkgs.runCommand "pip-purifier-nested-local-wheel" { } ''
  cp -r ${src}/. .
  chmod -R u+w .
  unset PYTHONPATH PYTHONHOME NIX_PYTHONPATH PIP_PURIFIER_BINDINGS PIP_PURIFIER_NATIVE_REPAIR PIP_PURIFIER_EXPORT_WHEELS
  ${pkgs.python3.interpreter} -I ${./nested-local-wheel.py} \
    --pip ${pip.interpreter} \
    --runner ${../pkgs/build-pip-project}/pip_runner.py \
    --wheelhouse ${project.inputs}/wheelhouse \
    --produced-wheel ${project.packages.greet}/greet-0.1.0-py3-none-any.whl \
    --out "$out"
''
