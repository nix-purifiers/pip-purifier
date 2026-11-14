# SPDX-License-Identifier: MIT
#
# readLock (lib.pip-purifier.readLock): pip-purifier.lock.json read into
# the lock in memory of the purifier contract, which the purifier library's
# checkLock and entriesFor take:
#
#   {
#     purifier = "pip-purifier";
#     entries  = the downloads: `artifacts`, and the files that each
#                pylock.toml among the inputs names; a wheel for one CPU
#                carries `systems`, the Nix systems of that CPU;
#     outputs  = the local projects, by name: { path, version, sdist,
#                wheels.<system> };
#     problems = why the lockfile cannot be used, each naming the command
#                that fixes it: missing, malformed, of another format
#                version, changed after the lock, or older than the
#                project's requirements;
#     lockfile = the lockfile as it is, for buildPipProject;
#   }
#
# The lockfile holds a hash of its own content: the SHA-256 of the
# lockfile without `hash`, as builtins.toJSON writes it (compact, keys
# sorted). The lock tool computes the same with Python's json module
# (lock_hash in src/pip_purifier.py).
{ lib }:
let
  inherit (builtins)
    attrNames
    filter
    hashFile
    toJSON
    ;

  supportedVersion = import ./lock-version.nix;
  abiVersion = import ./lock-abi-version.nix;

  # The top-level fields of a lockfile; LOCK_FIELDS in src/pip_purifier.py
  # lists the same.
  fields = [
    "version"
    "hash"
    "pip"
    "python"
    "args"
    "inputs"
    "pins"
    "versions"
    "outputs"
    "sources"
    "artifacts"
  ];

  # The Nix system of each CPU that a lock covers, as wheel platform tags
  # name it: Linux tags, and macOS tags, where a universal2 wheel holds
  # arm64 too.
  cpus = {
    x86_64 = "x86_64-linux";
    aarch64 = "aarch64-linux";
  };
  macosCpus = {
    arm64 = "aarch64-darwin";
    universal2 = "aarch64-darwin";
  };

  hashOf =
    lock:
    builtins.convertHash {
      hash = builtins.hashString "sha256" (toJSON (removeAttrs lock [ "hash" ]));
      hashAlgo = "sha256";
      toHashFormat = "sri";
    };

  # The systems a wheel is for, from the platform tags of its file name, or
  # null for a wheel that installs on every platform and for every other
  # download.
  systemsOf =
    path:
    let
      name = baseNameOf path;
      parts = lib.splitString "-" (lib.removeSuffix ".whl" name);
      platforms = lib.splitString "." (lib.last parts);
      systems = lib.unique (
        lib.concatMap (
          platform:
          let
            table = if lib.hasPrefix "macosx_" platform then macosCpus else cpus;
          in
          lib.concatMap (cpu: lib.optional (lib.hasSuffix "_${cpu}" platform) table.${cpu}) (attrNames table)
        ) platforms
      );
    in
    if !(lib.hasSuffix ".whl" name) || lib.elem "any" platforms then null else systems;

  # The wheels, sdists and archives that a pylock.toml names by URL, as
  # file entries, at the path that the offline install looks them up by:
  # the last segment of the URL, without its query or fragment. `artifacts`
  # holds the other downloads.
  pylockEntries =
    excluded: path:
    let
      data = builtins.fromTOML (builtins.readFile path);
      files =
        package:
        (package.wheels or [ ])
        ++ lib.optional (package ? sdist) package.sdist
        ++ lib.optional (package ? archive) package.archive;
      # The URL without credentials, as the lock tool records URLs.
      plain =
        url:
        lib.concatStrings (
          map (part: if builtins.isList part then builtins.head part else part) (
            builtins.split "^(https?://)[^/@]*@" url
          )
        );
      entry = file: {
        path = baseNameOf (builtins.head (builtins.split "[?#]" file.url));
        url = plain file.url;
        mirrors = [ ];
        hash = builtins.convertHash {
          hash = file.hashes.sha256;
          hashAlgo = "sha256";
          toHashFormat = "sri";
        };
      };
    in
    map entry (
      filter (file: file ? url) (
        lib.concatMap files (
          filter (
            package:
            !(lib.elem (lib.toLower (lib.replaceStrings [ "_" "." ] [ "-" "-" ] package.name)) excluded)
          ) (data.packages or [ ])
        )
      )
    );

  isPylock = rel: builtins.match "pylock(\\.[^/]+)?\\.toml" (baseNameOf rel) != null;

  problemOf =
    {
      lock,
      lockFile,
      resolve,
    }:
    let
      name = toString lockFile;
      missing = if builtins.isAttrs lock then filter (field: !(lock ? ${field})) fields else fields;
      stale = filter (
        rel: !(builtins.pathExists (resolve rel)) || hashFile "sha256" (resolve rel) != lock.inputs.${rel}
      ) (attrNames lock.inputs);
      pythonValid = builtins.isString lock.python && builtins.match "[0-9]+\\.[0-9]+" lock.python != null;
      primary = "cp" + builtins.replaceStrings [ "." ] [ "" ] lock.python;
      abiValid =
        pythonValid
        && builtins.isString (lock.pythonAbi or null)
        && builtins.elem lock.pythonAbi [
          primary
          (primary + "t")
        ];
    in
    if missing != [ ] then
      "pip-purifier: ${name} is not a pip-purifier lockfile: it lacks ${lib.concatStringsSep ", " missing}. Run `pip-purifier lock`."
    else if
      !(builtins.elem lock.version [
        supportedVersion
        abiVersion
      ])
    then
      "pip-purifier: ${name} has lock format version ${toString lock.version}; this library reads versions ${toString supportedVersion} and ${toString abiVersion}. Run `pip-purifier lock` with the pip-purifier that provides this library."
    else if lock.version == supportedVersion && lock ? pythonAbi then
      "pip-purifier: ${name} has ABI metadata in legacy format ${toString supportedVersion}. Run `pip-purifier lock`."
    else if lock.version == abiVersion && !abiValid then
      "pip-purifier: ${name} has missing or inconsistent Python ABI metadata. Run `pip-purifier lock`."
    else if lock.hash != hashOf lock then
      "pip-purifier: ${name} was changed after `pip-purifier lock` wrote it: its content does not match its hash. Run `pip-purifier lock`."
    else if stale != [ ] then
      "pip-purifier: ${name} does not match ${lib.concatStringsSep ", " stale} any more. Run `pip-purifier lock`."
    else
      null;
in
{
  inherit hashOf;

  readLock =
    {
      # The directory pip runs in.
      src,
      # The lockfile, which may lie outside `src`.
      lockFile ? src + "/pip-purifier.lock.json",
      # The lockfile's contents, parsed, in place of reading `lockFile`.
      lockData ? null,
      # Local projects outside `src` that the requirements name by a
      # relative path such as "../shared", each with its source.
      pathSources ? { },
      # pip's own lockfile (PEP 751). A copy outside `src` takes the place
      # of the one in `src`.
      pylock ? src + "/pylock.toml",
    }:
    let
      exists = lockData != null || builtins.pathExists lockFile;
      # Nix cannot catch a JSON parse error, so a lockfile that is not JSON
      # fails evaluation here, with this context naming the fix.
      lock =
        if lockData != null then
          lockData
        else
          builtins.addErrorContext "while reading ${toString lockFile}, which is not a valid lockfile. Run `pip-purifier lock`." (
            builtins.fromJSON (builtins.readFile lockFile)
          );

      # A path the lockfile names relative to `src`, which may lead into
      # one of the `pathSources`.
      resolve =
        rel:
        let
          keys = lib.sort (a: b: builtins.stringLength a > builtins.stringLength b) (
            filter (key: rel == key || lib.hasPrefix "${key}/" rel) (attrNames pathSources)
          );
          key = builtins.head keys;
        in
        if rel == "pylock.toml" then
          pylock
        else if keys == [ ] then
          src + "/${rel}"
        else if rel == key then
          pathSources.${key}
        else
          pathSources.${key} + "/${lib.removePrefix "${key}/" rel}";

      problem =
        if !exists then
          "pip-purifier: ${toString lockFile} does not exist. Run `pip-purifier lock`."
        else
          problemOf { inherit lock lockFile resolve; };
      usable = problem == null;

      # The artifacts, and the files of each pylock.toml among the inputs.
      # A file that two packages of a pylock.toml name is fetched once.
      downloads =
        lock.artifacts
        ++ lib.unique (
          lib.concatMap (rel: pylockEntries (lock.projectInputNames or [ ]) (resolve rel)) (
            filter isPylock (attrNames lock.inputs)
          )
        );
    in
    {
      purifier = "pip-purifier";
      entries =
        if usable then
          map (
            artifact:
            let
              systems = systemsOf artifact.path;
            in
            artifact // lib.optionalAttrs (systems != null) { inherit systems; }
          ) downloads
        else
          [ ];
      outputs = if usable then lock.outputs.packages else { };
      problems = lib.optional (!usable) problem;
      lockfile = lock;
      inherit resolve;
    };
}
