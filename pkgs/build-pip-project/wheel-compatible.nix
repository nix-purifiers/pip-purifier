# SPDX-License-Identifier: MIT
# Keep platform policy in purifier; validate Python/ABI pairs from the lock.
{ lib, purifier }:
{ pythonVersion, pythonAbi, ... }@args:
let
  parts = lib.splitString "." pythonVersion;
  major = builtins.head parts;
  minor = lib.toInt (builtins.elemAt parts 1);
  cp = "cp" + builtins.replaceStrings [ "." ] [ "" ] pythonVersion;
  free = lib.hasSuffix "t" pythonAbi;
  platformMatches = purifier.wheels (builtins.removeAttrs args [ "pythonAbi" ]);
  pythonMatches =
    tag: abi:
    let
      pure = builtins.match "(py|cp)${major}([0-9]*)" tag;
      stable = builtins.match "cp${major}([0-9]+)" tag;
      stableMinor = if stable == null then -1 else lib.toInt (builtins.head stable);
    in
    if abi == "none" then
      pure != null && (builtins.elemAt pure 1 == "" || lib.toInt (builtins.elemAt pure 1) <= minor)
    else if abi == (if free then "abi3t" else "abi3") then
      stableMinor >= (if free then 15 else 2) && stableMinor <= minor
    else
      abi == pythonAbi && tag == cp;
in
name:
let
  fields = lib.splitString "-" (lib.removeSuffix ".whl" name);
  count = builtins.length fields;
  tag = offset: builtins.elemAt fields (count - offset);
in
lib.hasSuffix ".whl" name
&& count >= 5
&& builtins.any (python: builtins.any (pythonMatches python) (lib.splitString "." (tag 2))) (
  lib.splitString "." (tag 3)
)
&& platformMatches ("probe-0-py${major}-none-" + tag 1 + ".whl")
