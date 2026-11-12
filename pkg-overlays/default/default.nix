# SPDX-License-Identifier: MIT
#
# The default package overlay. It adds one attribute to the package set,
# the scope `pkgs.pip-purifier`, which holds the packages of pkgs/ (`.cli`)
# and the builder `buildPipProject` (pkgs/build-pip-project/default.nix). The
# builder is a package of the scope for callPackage, and gets the
# `purifier` library of this flake's closure and `readLock` of its own
# library. `pip-purifier.overrideScope` replaces what the scope shares,
# and `buildPipProject.override` replaces what the builder takes. A
# consumer that lists this flake in `projects` holds the overlay as
# `pip-purifier/default`, which its package sets apply by default; the
# flake also exports it as `overlays.default`.
{ closure-lib, ... }:
let
  packages = closure-lib.caisson.nixpkgs.mkPackagesOverlay ../../pkgs "pip-purifier";
in
{
  overlay =
    final: prev:
    let
      added = packages final prev;
    in
    added
    // {
      pip-purifier = added.pip-purifier.overrideScope (
        self: super: {
          buildPipProject = self.callPackage ../../pkgs/build-pip-project/default.nix {
            inherit (closure-lib) purifier;
            inherit (closure-lib.pip-purifier) readLock;
          };
        }
      );
    };
}
