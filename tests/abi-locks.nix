# Lock/SDK identity is checked even when no wheel contains extension code.
{ pkgs, buildPipProject }:
let
  reader = import ../lib-overlays/default/read-lock.nix { inherit (pkgs) lib; };
  src = ./fixtures/hashed;
  legacy = builtins.fromJSON (builtins.readFile (src + "/pip-purifier.lock.json"));
  primary = "cp" + builtins.replaceStrings [ "." ] [ "" ] legacy.python;
  regular = pkgs.${"python" + builtins.replaceStrings [ "." ] [ "" ] legacy.python};
  free = pkgs.${"python" + builtins.replaceStrings [ "." ] [ "" ] legacy.python + "FreeThreading"};
  abi = legacy // {
    version = 5;
    pythonAbi = primary + "t";
  };
  seal = data: data // { hash = reader.hashOf data; };
  problems =
    data:
    (reader.readLock {
      inherit src;
      lockData = seal data;
    }).problems;
  usable =
    data: python:
    (builtins.tryEval (
      builtins.deepSeq
        (buildPipProject {
          inherit src python;
          lockData = seal data;
        }).inputs.drvPath
        true
    )).success;
in
assert builtins.all (entry: pkgs.lib.hasSuffix "-py3-none-any.whl" entry.path) legacy.artifacts;
assert regular.pythonVersion == free.pythonVersion;
assert builtins.elem primary regular.pythonABITags;
assert builtins.elem (primary + "t") free.pythonABITags;
assert problems legacy == [ ];
assert problems abi == [ ];
assert problems (builtins.removeAttrs abi [ "pythonAbi" ]) != [ ];
assert problems (abi // { pythonAbi = "abi3t"; }) != [ ];
assert problems (abi // { pythonAbi = "cp999t"; }) != [ ];
assert problems (legacy // { pythonAbi = primary; }) != [ ];
assert usable legacy regular;
assert usable abi free;
assert !(usable legacy free);
assert !(usable abi regular);
pkgs.runCommand "pip-purifier-abi-locks" { } ''touch "$out"''
