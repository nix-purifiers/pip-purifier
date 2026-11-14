# SPDX-License-Identifier: MIT
# Internal locked artifact view. A local workflow supplies no project SDK.
{
  lib,
  stdenv,
  runCommand,
  writeText,
  fetchurl,
  fetchgit,
  fetchhg,
  fetchsvn,
  fetchbzr,
  unzip,
  zstd,
  python3,
  patchelf,
  purifier,
}:
{
  checked,
  read,
  lock,
  fetchers ? { },
  projectDependencies ? null,
  python ? null,
  pythonTags ? null,
  runtimeLibraries ? [ ],
}:
let
  inherit (builtins) filter toJSON;
  inherit (lib) hasSuffix concatMapStrings escapeShellArg;
  system = stdenv.hostPlatform.system;
  arch = stdenv.hostPlatform.parsed.cpu.name;
  inputRecords = checked.projectInputs or [ ];
  dependencies =
    if projectDependencies == null then
      purifier.lockedProjectInputs {
        inputs = inputRecords;
        inherit system;
      }
    else
      projectDependencies;
  supplied = purifier.projectInputs {
    inherit dependencies;
    formats = [ "python" ];
  };
  inputViews = writeText "pip-python-views.json" (
    toJSON (
      map (input: {
        repository = toString input.repository;
        catalog = input.repository.catalog;
      }) supplied
    )
  );
  inputBindings =
    assert !(python != null && pythonTags != null);
    runCommand "pip-python-bindings"
      {
        nativeBuildInputs = [ (python3.withPackages (ps: [ ps.packaging ])) ];
      }
      ''
        ${lib.optionalString (python != null) ''
          ${python3.interpreter} -I ${./python_identity.py} \
            ${lib.escapeShellArg python.interpreter} \
            ${lib.escapeShellArg checked.python} \
            ${lib.escapeShellArg (
              checked.pythonAbi or ("cp" + builtins.replaceStrings [ "." ] [ "" ] checked.python)
            )}
        ''}
        python ${./python_bindings.py} --tools ${purifier.projectInputsTool} \
          --views ${inputViews} --old-repositories ${writeText "old-repositories.json" "[]"} --destination "$out" \
          ${
            if python != null then
              "--python " + lib.escapeShellArg python.interpreter
            else if pythonTags != null then
              "--tags " + toString (writeText "consumer-python-tags.json" (toJSON pythonTags))
            else
              ""
          }
      '';

  fetchArgs = {
    inherit
      fetchurl
      fetchgit
      fetchhg
      fetchsvn
      fetchbzr
      runCommand
      unzip
      zstd
      python3
      writeText
      ;
  }
  // fetchers;
  fetch = purifier.fetchEntry fetchArgs;
  compatible = import ./wheel-compatible.nix { inherit lib purifier; } (
    {
      pythonVersion = checked.python;
      pythonAbi = checked.pythonAbi or ("cp" + builtins.replaceStrings [ "." ] [ "" ] checked.python);
    }
    // (
      if stdenv.hostPlatform.isDarwin then
        {
          # macOS wheel tags name the CPU arm64, and the oldest macOS that a
          # wheel runs on, which is at most nixpkgs' darwinMinVersion.
          arch = "arm64";
          macosVersion = stdenv.hostPlatform.darwinMinVersion;
        }
      else
        {
          inherit arch;
          glibcVersion = lib.versions.majorMinor (lib.getVersion stdenv.cc.libc);
        }
    )
  );

  isFile = entry: entry ? url;
  fileName = entry: baseNameOf entry.path;
  # The entries of this system, of which the build keeps the wheels that
  # install here, and every sdist, archive and checkout.
  files = filter (
    entry: isFile entry && (!(hasSuffix ".whl" entry.path) || compatible (fileName entry))
  ) (builtins.seq checked (purifier.entriesFor system read));
  byPath = lib.listToAttrs (
    map (entry: {
      name = entry.path;
      value = entry;
    }) lock.entries
  );

  # Each VCS or URL requirement, as written, to its local copy: a checkout
  # in the store, or a file of the wheelhouse (relative to `inputs`).
  sources = lib.mapAttrs (
    _: path:
    let
      entry = byPath.${path};
    in
    if isFile entry then "wheelhouse/${fileName entry}" else "${fetch entry}"
  ) checked.sources;

  # The path at which `mitm-cache replay` looks up a URL: "https/" or
  # "http/", then the host and path, without the query.
  replayPath =
    url:
    let
      m = builtins.match "(https?)://([^?#]*).*" url;
    in
    "${builtins.elemAt m 0}/${builtins.elemAt m 1}";

  # Each git repository that another package names as a source, with a
  # clone of it in the store: the purifier library rebuilds its .git from
  # the recorded commit object.
  repositories = map (repository: repository // { path = "${fetch byPath.${repository.path}}"; }) (
    checked.repositories or [ ]
  );

  # The wheels that install here and every sdist and archive, by file name.
  wheelhouse = purifier.tree (
    fetchArgs
    // {
      name = "pip-purifier-wheelhouse";
      entries = files;
    }
  );

  # Each Mercurial, Subversion or Bazaar repository that another package
  # names as a source, with its locked checkout, for vcs_shim.py.
  shims = map (shim: shim // { path = "${fetch byPath.${shim.path}}"; }) (checked.shims or [ ]);

  inputs = runCommand "pip-purifier-inputs" { } ''
    ${lib.optionalString (python != null) ''
      ${python3.interpreter} -I ${./python_identity.py} \
        ${lib.escapeShellArg python.interpreter} \
        ${lib.escapeShellArg checked.python} \
        ${lib.escapeShellArg (
          checked.pythonAbi or ("cp" + builtins.replaceStrings [ "." ] [ "" ] checked.python)
        )}
    ''}
    mkdir -p $out
    ${lib.optionalString (python != null) "ln -s ${python} $out/python"}
    cp ${
      writeText "python-identity.json" (toJSON {
        python = checked.python;
        pythonAbi = checked.pythonAbi or ("cp" + builtins.replaceStrings [ "." ] [ "" ] checked.python);
      })
    } $out/python-identity.json
    ${lib.optionalString (python != null && stdenv.hostPlatform.isLinux) ''
      cp ${
        writeText "native-runtime.json" (toJSON {
          patchelf = "${patchelf}/bin/patchelf";
          dynamicLinker = stdenv.cc.bintools.dynamicLinker;
          libraryPath = lib.makeLibraryPath runtimeLibraries;
        })
      } $out/native-runtime.json
    ''}
    ${lib.optionalString (supplied != [ ]) "ln -s ${inputBindings}/inputs.json $out/python-inputs.json"}
    ln -s ${wheelhouse} $out/wheelhouse
    mkdir -p $out/replay
    ${concatMapStrings (replay: ''
      mkdir -p "$(dirname $out/replay/${escapeShellArg (replayPath replay.url)})"
      ln -s ${fetch byPath.${replay.path}} $out/replay/${escapeShellArg (replayPath replay.url)}
    '') (checked.replays or [ ])}
    cp ${writeText "sources.json" (toJSON sources)} $out/sources.json
    cp ${writeText "repositories.json" (toJSON repositories)} $out/repositories.json
    cp ${writeText "shims.json" (toJSON shims)} $out/shims.json
    cp ${writeText "versions.json" (toJSON checked.versions)} $out/versions.json
    cp ${writeText "args.json" (toJSON checked.args)} $out/args.json
    cp ${writeText "pins.txt" (lib.concatLines checked.pins.${system})} $out/pins.txt
  '';

in
{
  inherit inputs dependencies;
}
