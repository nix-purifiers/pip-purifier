# SPDX-License-Identifier: MIT
#
# The conformance checks of the purifier contract that run in the sandbox,
# on the hashed fixture. A bad lockfile is given as `lockData`, so that
# evaluation reads no generated file. Each check asserts that evaluating
# the build fails, with builtins.tryEval, and that the message the build
# gives in `problems` (lib-overlays/default/read-lock.nix) names the problem and the command that
# fixes it. A failure inside a build is caught with testers.testBuildFailure,
# whose log the check reads.
{
  pkgs,
  buildPipProject,
}:
let
  inherit (pkgs) lib runCommand;
  lockCheck = import ../lib-overlays/default/read-lock.nix { inherit lib; };

  src = ./fixtures/hashed;
  lock = builtins.fromJSON (builtins.readFile (src + "/pip-purifier.lock.json"));

  # A changed lockfile with its hash made to match, as if the lock tool had
  # written it.
  rehashed = changed: changed // { hash = lockCheck.hashOf changed; };

  withLock =
    lockData:
    buildPipProject {
      inherit src lockData;
    };

  fails =
    name: value:
    let
      result = builtins.tryEval (builtins.seq value.drvPath true);
    in
    lib.assertMsg (!result.success) "pip-purifier: the case ${name} evaluated, and it should not";

  # The message for a lockfile, which has to name `expected` and the
  # command that fixes it.
  says = name: lockData: saysFor name { inherit lockData; };
  saysFor =
    name: args: expected:
    let
      problems = (lockCheck.readLock ({ inherit src; } // args)).problems;
      problem = if problems == [ ] then null else builtins.head problems;
    in
    lib.assertMsg (
      problem != null && lib.hasInfix expected problem && lib.hasInfix "Run `pip-purifier lock`" problem
    ) "pip-purifier: the case ${name} gave the message ${builtins.toJSON problem}";

  # Passes when every case fails as it should, and records the messages.
  check =
    name: cases:
    assert lib.all (case: case) cases;
    runCommand "pip-purifier-conformance-${name}" { } "touch $out";

  # The requirements changed after the lock: their hash no longer matches.
  stale = rehashed (lock // { inputs = lib.mapAttrs (_: _: lib.fakeSha256) lock.inputs; });

  # A download whose hash was edited by hand.
  tampered = lock // {
    artifacts = map (
      artifact: artifact // { hash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="; }
    ) lock.artifacts;
  };

  # A lockfile whose download of idna was removed: pip cannot find idna
  # offline, and the error names it.
  missing = rehashed (lock // { artifacts = [ ]; });
  missingFailure = pkgs.testers.testBuildFailure (withLock missing).environment;

  lacksArtifacts = removeAttrs lock [ "artifacts" ];
  newerFormat = rehashed (lock // { version = 999; });

  # The fixture's requirements, with no lockfile beside them.
  withoutLockfile = lib.fileset.toSource {
    root = src;
    fileset = src + "/requirements.txt";
  };
in
{
  conformance-stale = check "stale" [
    (fails "stale" (withLock stale).environment)
    (says "stale" stale "does not match requirements.txt any more")
  ];

  conformance-tampered = check "tampered" [
    (fails "tampered" (withLock tampered).environment)
    (says "tampered" tampered "was changed after `pip-purifier lock` wrote it")
  ];

  conformance-malformed = check "malformed" [
    (fails "lacks-artifacts" (withLock lacksArtifacts).environment)
    (says "lacks-artifacts" lacksArtifacts "is not a pip-purifier lockfile: it lacks artifacts")
    (fails "not-an-object" (withLock [ "a" ]).environment)
    (says "not-an-object" [ "a" ] "is not a pip-purifier lockfile")
    (fails "newer-format" (withLock newerFormat).environment)
    (says "newer-format" newerFormat "has lock format version 999")
    (fails "no-lockfile" (buildPipProject { src = withoutLockfile; }).environment)
    (saysFor "no-lockfile" { src = withoutLockfile; } "does not exist")
  ];

  conformance-missing = runCommand "pip-purifier-conformance-missing" { } ''
    grep -q 'the lockfile has no file for idna' ${missingFailure}/testBuildFailure.log
    grep -q 'run `pip-purifier lock`' ${missingFailure}/testBuildFailure.log
    touch $out
  '';

  # A lockfile for another system or another Python.
  other-system-or-python = check "other-system-or-python" [
    (fails "other-system" (withLock (rehashed (lock // { pins.riscv64-linux = [ ]; }))).environment)
    (fails "other-python"
      (buildPipProject {
        inherit src;
        python = pkgs.python312;
      }).environment
    )
  ];
}
