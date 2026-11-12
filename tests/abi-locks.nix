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
  wheel =
    abi:
    import ../pkgs/build-pip-project/wheel-compatible.nix
      {
        inherit (pkgs) lib;
        # Platform behavior is delegated unchanged; isolate ABI combinations here.
        purifier.wheels = _: _: true;
      }
      {
        pythonVersion = "3.15";
        pythonAbi = abi;
        arch = "x86_64";
        glibcVersion = "2.40";
      };
in
assert builtins.all (entry: pkgs.lib.hasSuffix "-py3-none-any.whl" entry.path) legacy.artifacts;
assert regular.pythonVersion == free.pythonVersion;
assert builtins.elem primary regular.pythonABITags;
assert builtins.elem (primary + "t") free.pythonABITags;
assert wheel "cp315" "probe-0-cp315-cp315-manylinux_2_17_x86_64.whl";
assert !(wheel "cp315t" "probe-0-cp315-cp315-manylinux_2_17_x86_64.whl");
assert wheel "cp315t" "probe-0-cp315-cp315t-manylinux_2_17_x86_64.whl";
assert !(wheel "cp315" "probe-0-cp315-cp315t-manylinux_2_17_x86_64.whl");
assert wheel "cp315" "probe-0-cp312-abi3-manylinux_2_17_x86_64.whl";
assert !(wheel "cp315t" "probe-0-cp312-abi3-manylinux_2_17_x86_64.whl");
assert wheel "cp315t" "probe-0-cp315-abi3t-manylinux_2_17_x86_64.whl";
assert !(wheel "cp315" "probe-0-cp315-abi3t-manylinux_2_17_x86_64.whl");
assert !(wheel "cp315t" "probe-0-cp314-abi3t-manylinux_2_17_x86_64.whl");
assert wheel "cp315t" "probe-0-py3-none-any.whl";
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
