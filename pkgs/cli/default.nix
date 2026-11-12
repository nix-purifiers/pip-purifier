# SPDX-License-Identifier: MIT
#
# The `pip-purifier` command. It runs the pip of this package set, which is
# also the pip that buildPipProject runs, so the lock run resolves with the
# pip that will install offline. `prefetch`, `pip` and `publish` build with
# the Nix library and the nixpkgs of this package set.
{
  lib,
  stdenvNoCC,
  makeWrapper,
  python3,
  git,
  mercurial,
  subversion,
  breezy,
  mitm-cache,
  openssl,
  uv,
  path,
}:
let
  # The purifier library at the revision this flake's lock pins. The
  # package links it, so a root on the package keeps it for pkgs/cli/cli.nix.
  flakeLock = builtins.fromJSON (builtins.readFile ../../flake.lock);
  purifierSource = builtins.fetchTree flakeLock.nodes.${flakeLock.nodes.root.inputs.purifier}.locked;

  python = python3.withPackages (ps: [
    ps.pip
    ps.build
    ps.packaging
  ]);
in
stdenvNoCC.mkDerivation {
  pname = "pip-purifier";
  version = import ../version.nix;

  src = lib.fileset.toSource {
    root = ../..;
    fileset = lib.fileset.unions [
      ../../src
      ../build-pip-project
      ./cli.nix
      ../../lib-overlays/default
    ];
  };

  nativeBuildInputs = [ makeWrapper ];

  installPhase = ''
    runHook preInstall
    mkdir -p $out/bin $out/share/pip-purifier
    cp src/pip_purifier.py $out/share/pip-purifier/
    mkdir -p $out/share/pip-purifier/pkgs/cli
    cp -r pkgs/build-pip-project $out/share/pip-purifier/pkgs/
    cp pkgs/cli/cli.nix $out/share/pip-purifier/pkgs/cli/
    cp -r lib-overlays $out/share/pip-purifier/
    ln -s ${purifierSource} $out/share/pip-purifier/purifier
    makeWrapper ${python.interpreter} $out/bin/pip-purifier \
      --add-flags "-I $out/share/pip-purifier/pip_purifier.py" \
      --prefix PATH : ${
        lib.makeBinPath [
          git
          mercurial
          subversion
          breezy
          mitm-cache
          openssl
          uv
        ]
      } \
      --set PIP_PURIFIER_PROJECT_INPUTS_TOOLS ${purifierSource}/tools \
      --set PIP_PURIFIER_NIX_LIB $out/share/pip-purifier/pkgs/build-pip-project \
      --set PIP_PURIFIER_NIXPKGS ${path}
    runHook postInstall
  '';

  meta = {
    description = "Lock, install and publish pip projects that Nix builds";
    license = lib.licenses.mit;
    mainProgram = "pip-purifier";
  };
}
