# Immutable upstream installer wheel; target CPython is selected separately.
{
  repo ? null,
  flake ? builtins.getFlake repo,
  system,
}:
let
  f = flake;
  pkgs = import f.inputs.nixpkgs {
    inherit system;
    overlays = [ f.overlays.default ];
  };
  python = pkgs.python3.override {
    packageOverrides = final: prev: {
      pip = final.buildPythonPackage {
        pname = "pip";
        version = "26.3";
        format = "wheel";
        src = pkgs.fetchurl {
          url = "https://files.pythonhosted.org/packages/07/25/ee8c38ae72d66b844ce8eb9711ba905dba21ca1a8a0c960057c41cd23337/pip-26.3-py3-none-any.whl";
          sha256 = "f3b60b20ffaadd4415ed7957212a38ac63fa4c5fa375bd0f4a797777940fad13";
        };
        pythonImportsCheck = [ "pip" ];
      };
    };
  };
in
pkgs.pip-purifier.cli.override { python3 = python; }
