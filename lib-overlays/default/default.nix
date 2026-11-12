# SPDX-License-Identifier: MIT
#
# The `pip-purifier` library namespace: `readLock` (lib-overlays/default/read-lock.nix).
# A consumer that lists this flake in `projects` holds it as
# `pip-purifier/default`, and its library then has `lib.pip-purifier`. The
# builder needs a package set, so it is the package `buildPipProject` that
# the default package overlay adds.
{ ... }:
{

  overlay = final: prev: {
    pip-purifier = {
      inherit (import ./read-lock.nix { lib = final; }) readLock;
    };
  };

}
