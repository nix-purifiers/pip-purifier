# SPDX-License-Identifier: MIT
#
# What the `pip-purifier` command builds for a project directory and a
# lockfile, each given as an absolute path, with the nixpkgs the command was
# built with: `attr` is "inputs", "release" or "packages.<name>".
#
# A local project that the requirements name by a path outside the project
# directory is read from that path.
{
  nixpkgs,
  src,
  lockFile,
  # A pylock.toml kept outside the project, as an absolute path, or "" for
  # the project's own.
  pylock,
  attr,
}:
let
  pkgs = import nixpkgs { };
  inherit (pkgs) lib;

  # A directory without what pip, Python, git and pip-purifier leave in it.
  source =
    path:
    builtins.path {
      inherit path;
      name = "source";
      filter =
        p: type:
        !(lib.hasSuffix ".egg-info" (baseNameOf p))
        && !builtins.elem (baseNameOf p) [
          ".git"
          ".venv"
          ".pip-purifier"
          "__pycache__"
          ".pytest_cache"
          ".mypy_cache"
          ".tox"
          ".nox"
          "build"
          "dist"
        ];
    };

  root = /. + src;
  lockPath = /. + lockFile;
  lock = builtins.fromJSON (builtins.readFile lockPath);
  outside = lib.unique (
    map dirOf (builtins.filter (rel: lib.hasPrefix "../" rel) (builtins.attrNames lock.inputs))
  );
  pathSources = lib.genAttrs outside (rel: source (/. + "${src}/${rel}"));

  # The purifier library at the revision this pip-purifier's flake.lock
  # pins, which the command's package links beside this directory.
  purifier = import ../../purifier/nix/lib { inherit lib; };

  inherit (import ../../lib-overlays/default/read-lock.nix { inherit lib; }) readLock;
  validated = purifier.checkLock (
    readLock (
      {
        src = source root;
        lockFile = lockPath;
        lockData = lock;
        inherit pathSources;
      }
      // lib.optionalAttrs (pylock != "") { pylock = /. + pylock; }
    )
  );
  localInputs = (pkgs.callPackage ../build-pip-project/locked-artifacts.nix { inherit purifier; }) {
    checked = lock;
    read = readLock (
      {
        src = source root;
        lockFile = lockPath;
        lockData = lock;
        inherit pathSources;
      }
      // lib.optionalAttrs (pylock != "") { pylock = /. + pylock; }
    );
    lock = validated;
  };
  project = builtins.seq validated (
    pkgs.callPackage ../build-pip-project/default.nix { inherit purifier readLock; } (
      {
        src = source root;
        lockFile = lockPath;
        lockData = lock;
        python =
          pkgs.${
            "python"
            + builtins.replaceStrings [ "." ] [ "" ] lock.python
            + lib.optionalString (lib.hasSuffix "t" (lock.pythonAbi or "")) "FreeThreading"
          }
            or (throw "pip-purifier: nixpkgs has no Python ${lock.python} with ABI ${lock.pythonAbi or "regular"}");
        inherit pathSources;
      }
      // lib.optionalAttrs (pylock != "") {
        pylock = /. + pylock;
      }
    )
  );
in
if attr == "localInputs" then
  builtins.seq validated localInputs.inputs
else if
  lock.projectInputs or [ ] != [ ] && (attr == "release" || lib.hasPrefix "packages." attr)
then
  (
    if attr == "release" then
      project
    else
      lib.attrByPath (lib.splitString "." attr) (throw "pip-purifier: the project has no output ${attr}")
        project
  ).projections.python
else
  lib.attrByPath (lib.splitString "." attr) (throw "pip-purifier: the project has no output ${attr}")
    project
