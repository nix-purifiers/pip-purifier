# SPDX-License-Identifier: MIT
#
# The flake's checks build in the sandbox. Locked downloads use fixed-output
# fetches; the two loopback checks run their own local test servers.
{
  pkgs,
  # The default package overlay of the flake, which adds the scope
  # pip-purifier with buildPipProject.
  overlay,
  cli,
}:
let
  inherit (pkgs) lib runCommand;
  # These two checks run their own loopback servers. Keep all other
  # derivations unchanged and retain the external-network denial control.
  loopback = lib.optionalAttrs pkgs.stdenv.hostPlatform.isDarwin {
    __darwinAllowLocalNetworking = true;
  };

  # Every fixture builds the way a consumer's package does: through
  # pip-purifier.buildPipProject of a package set that the overlay applies
  # to.
  build = (pkgs.extend overlay).pip-purifier.buildPipProject;

  # A local setuptools project installed with `-e .`, with a console
  # script, a wheel, an sdist-only dependency (docopt, built with the locked
  # setuptools) and a dependency with platform wheels (PyYAML).
  project = build { src = ./fixtures/project; };

  nativeNode = import ./native-runtime-node.nix {
    inherit pkgs;
    environment = project.environment;
  };

  # Native pip understands editable extras; its report must also establish
  # package ownership and track the local project's manifest.
  editableExtras = build { src = ./fixtures/editable-extras; };

  nativeTestTool = pkgs.writeShellScriptBin "purifier-native-test-tool" ''
    python -I -c 'import attrs; assert attrs.__version__ == "25.3.0"'
  '';
  withNativeTestTool = build {
    src = ./fixtures/project;
    nativeCheckInputs = [ nativeTestTool ];
    checkPhase = "purifier-native-test-tool";
  };

  # A git requirement whose version comes from git (attrs, with hatch-vcs),
  # a URL archive (six), a local directory and a nested requirements file.
  # The local directory depends on a git repository (iniconfig, also
  # versioned with hatch-vcs) and on a URL (pyparsing) through its own
  # metadata.
  sources = build { src = ./fixtures/sources; };

  # Exercise networking in the same derivation that installs recorded URL
  # sources, then preserve the receipt alongside the installed environment.
  sourcesReplayEnvironment = sources.environment.overrideAttrs (old: {
    meta = (old.meta or { }) // {
      mainProgram = "sources";
    };
    preBuild = (old.preBuild or "") + ''
      ${pkgs.python3.interpreter} ${./network_check.py} > $TMPDIR/replay-network.json
    '';
    postBuild = (old.postBuild or "") + ''
      mkdir -p $out/nix-support
      cp $TMPDIR/replay-network.json $out/nix-support/replay-network.json
    '';
  });

  # A pylock.toml that `pip lock` wrote.
  pylock = build { src = ./fixtures/pylock; };

  # The PEP 723 dependencies of a script, tested with `checkPhase`.
  script = build {
    src = ./fixtures/script;
    checkPhase = ''
      output=$(python app.py)
      echo "$output"
      [ "$output" = "script six=1.17.0 packaging=25.0" ]
    '';
  };

  # A requirements file with --hash pins, as pip-compile writes it.
  hashed = build { src = ./fixtures/hashed; };

  # A local package that names a Mercurial, a Subversion and a Bazaar
  # repository as sources in its own metadata, installed offline with the
  # shims answering pip's clones from locked checkouts. The checkouts are
  # built here, and the setuptools wheel comes from the project fixture.
  shimPackage =
    name:
    pkgs.writeTextDir "pyproject.toml" ''
      [build-system]
      requires = ["setuptools>=70"]
      build-backend = "setuptools.build_meta"

      [project]
      name = "${name}"
      version = "1.0"

      [tool.setuptools]
      py-modules = []
    '';
  shimInputs = runCommand "pip-purifier-test-shim-inputs" { } ''
    mkdir -p $out/user
    ln -s ${project.inputs}/wheelhouse $out/wheelhouse
    cat > $out/user/pyproject.toml <<'EOF'
    [build-system]
    requires = ["setuptools>=70"]
    build-backend = "setuptools.build_meta"

    [project]
    name = "shimuser"
    version = "1.0"
    dependencies = [
      "shimhg @ hg+https://hg.example.invalid/shimhg@0123456789abcdef0123456789abcdef01234567",
      "shimsvn @ svn+https://svn.example.invalid/shimsvn@7",
      "shimbzr @ bzr+https://bzr.example.invalid/shimbzr@3",
    ]

    [tool.setuptools]
    py-modules = []
    EOF
    # The user package, and a top-level Subversion requirement, which the
    # rewriter points at its locked checkout.
    echo '["./user", "shimtop @ svn+https://svn.example.invalid/shimtop@5"]' > $out/args.json
    echo '{"svn+https://svn.example.invalid/shimtop@5": "${shimPackage "shimtop"}"}' > $out/sources.json
    echo '{}' > $out/versions.json
    echo '[]' > $out/repositories.json
    touch $out/pins.txt
    cat > $out/shims.json <<'EOF'
    [
      {"vcs": "hg", "url": "https://hg.example.invalid/shimhg", "rev": "0123456789abcdef0123456789abcdef01234567", "version": "1.0", "path": "${shimPackage "shimhg"}"},
      {"vcs": "svn", "url": "https://svn.example.invalid/shimsvn", "rev": "7", "version": "1.0", "path": "${shimPackage "shimsvn"}"},
      {"vcs": "bzr", "url": "https://bzr.example.invalid/shimbzr", "rev": "3", "version": "1.0", "path": "${shimPackage "shimbzr"}"}
    ]
    EOF
  '';
  shims =
    runCommand "pip-purifier-test-shims"
      {
        nativeBuildInputs = [
          (pkgs.python3.withPackages (ps: [ ps.pip ]))
          pkgs.git
        ];
      }
      ''
        export HOME=$TMPDIR
        cp -r ${shimInputs}/user user
        chmod -R u+w user
        python -m venv --without-pip venv
        python ${../pkgs/build-pip-project}/offline.py --project $PWD --inputs ${shimInputs} --venv venv --no-editable
        # Each package is installed, and pip recorded the locked revision
        # that the shim reported.
        venv/bin/python - <<'EOF'
        import importlib.metadata as m
        import json

        for name, rev in [
            ("shimhg", "0123456789abcdef0123456789abcdef01234567"),
            ("shimsvn", "7"),
            ("shimbzr", "3"),
        ]:
            direct = json.loads(m.distribution(name).read_text("direct_url.json"))
            print(name, m.version(name), direct["vcs_info"])
            assert direct["vcs_info"]["commit_id"] == rev, direct
        print("shimtop", m.version("shimtop"))
        EOF
        touch $out
      '';

  # Runs a program and compares what it prints.
  runs =
    name: program: expected:
    runCommand "pip-purifier-test-${name}" { } ''
      output=$(${lib.getExe program})
      echo "$output"
      if [ "$output" != ${lib.escapeShellArg expected} ]; then
        echo "expected: "${lib.escapeShellArg expected} >&2
        exit 1
      fi
      touch $out
    '';

  # Runs a Python snippet in an environment and compares what it prints.
  imports =
    name: environment: code: expected:
    runCommand "pip-purifier-test-${name}" { } ''
      output=$(${environment}/bin/python -c ${lib.escapeShellArg code})
      echo "$output"
      if [ "$output" != ${lib.escapeShellArg expected} ]; then
        echo "expected: "${lib.escapeShellArg expected} >&2
        exit 1
      fi
      touch $out
    '';

  # The development loop with no network, which the sandbox has none of: runs
  # `pip-purifier prefetch` in a copy of a fixture, with the inputs Nix
  # built for it in place of its own nix-build, then runs a program of the
  # .venv. `edit` changes the source afterwards: an editable install prints
  # the change.
  offline =
    fixture: inputs: program: expected: edit: edited:
    runCommand "pip-purifier-conformance-offline"
      {
        nativeBuildInputs = [
          cli
          pkgs.python3
        ];
      }
      ''
        cp -r ${fixture} project
        chmod -R u+w project
        cd project
        export HOME=$TMPDIR
        pip-purifier prefetch --inputs ${inputs}
        check() {
          output=$(.venv/bin/${program})
          echo "$output"
          if [ "$output" != "$1" ]; then
            echo "expected: $1" >&2
            exit 1
          fi
        }
        check ${lib.escapeShellArg expected}
        ${edit}
        check ${lib.escapeShellArg edited}
        touch $out
      '';

  # The hashed fixture without its lockfile, built from the lockfile kept
  # outside it, as a packaging flake builds a project it does not change.
  external = build {
    src = lib.fileset.toSource {
      root = ./fixtures/hashed;
      fileset = lib.fileset.difference ./fixtures/hashed ./fixtures/hashed/pip-purifier.lock.json;
    };
    lockFile = ./fixtures/hashed/pip-purifier.lock.json;
  };

  # A tree whose pylock.toml is out of date, built with both lockfiles of
  # the pylock fixture kept outside it.
  externalPylock = build {
    src = ./fixtures/stale-pylock;
    lockFile = ./fixtures/pylock/pip-purifier.lock.json;
    pylock = ./fixtures/pylock/pylock.toml;
  };
in
{
  composition-producer = project.packages.greet;
  native-check-inputs =
    assert withNativeTestTool.environment.drvPath == project.environment.drvPath;
    assert withNativeTestTool.packages.greet.drvPath == project.packages.greet.drvPath;
    assert withNativeTestTool.release.drvPath == project.release.drvPath;
    withNativeTestTool.tests;
  editable-extras =
    runCommand "pip-editable-extras-capture-replay-release"
      (
        {
          nativeBuildInputs = [
            cli
            pkgs.python3
          ];
        }
        // loopback
      )
      ''
        export HOME=$TMPDIR
        ${pkgs.python3.interpreter} ${./network_check.py} > network.json
        cp -r ${./fixtures/editable-extras} project
        chmod -R u+w project
        cd project
        rm pip-purifier.lock.json
        pip-purifier lock --system x86_64-linux --system aarch64-linux --system aarch64-darwin
        cmp pip-purifier.lock.json ${./fixtures/editable-extras/pip-purifier.lock.json}
        pip-purifier prefetch --inputs ${editableExtras.inputs}
        .venv/bin/python -I -c 'import editable_extras; assert editable_extras.VALUE == "owned-local-extra"'
        ${editableExtras.environment}/bin/python -I -c 'import editable_extras; assert editable_extras.VALUE == "owned-local-extra"'
        test -f ${editableExtras.release}/editable_extras-1.0.0-py3-none-any.whl
        test -f ${editableExtras.release}/editable_extras-1.0.0.tar.gz
        # A local manifest edit must invalidate this lock before replay.
        echo '# changed after capture' >> pyproject.toml
        if pip-purifier prefetch >stale.log 2>&1; then
          echo 'edited local manifest was not rejected' >&2
          exit 1
        fi
        grep -q "does not match the project's requirements" stale.log
        mkdir -p $out
        cp ../network.json $out/network.json
      '';
  installer-sdk-isolation = import ./installer-sdk.nix { inherit pkgs; };

  python-projection =
    runCommand "pip-native-projection-downstream"
      {
        nativeBuildInputs = [
          pkgs.uv
          pkgs.python3
        ];
      }
      ''
        export HOME=$TMPDIR UV_CACHE_DIR=$TMPDIR/uv-cache UV_PYTHON_DOWNLOADS=never
        uv venv --python ${pkgs.python3.interpreter} downstream
        uv pip install --python downstream/bin/python --no-index \
          --find-links ${project.packages.greet.projections.python} greet
        test "$(downstream/bin/greet)" = 'hello attrs=25.3.0 docopt=0.6.2 yaml=6.0.3'
        downstream/bin/python - <<'PYTHON'
        from importlib.metadata import version
        import json
        assert '+nix.' in version('greet')
        catalog = json.load(open('${project.packages.greet.projections.python.catalog}'))
        assert sorted(module['name'] for module in catalog['modules']) == ['attrs', 'docopt', 'greet', 'pyyaml']
        PYTHON
        touch $out
      '';

  project = runs "project" project.programs.greet "hello attrs=25.3.0 docopt=0.6.2 yaml=6.0.3";

  # The local project's sdist and wheel, under the names the lockfile
  # gives them, and the release that holds them.
  project-package = runCommand "pip-purifier-test-project-package" { } ''
    test -f ${project.packages.greet.files."greet-0.1.0-py3-none-any.whl"}
    test -f ${project.packages.greet.files."greet-0.1.0.tar.gz"}
    test -f ${project.release}/greet-0.1.0-py3-none-any.whl
    test -f ${project.release}/greet-0.1.0.tar.gz
    touch $out
  '';

  # The download entries of the lock: the fixture locks a PyYAML wheel for
  # each system, which carries that system in `systems`, and a wheel for
  # every platform, which carries none.
  fetches =
    let
      systemsOf =
        prefix:
        map (entry: entry.systems or null) (
          lib.filter (entry: lib.hasPrefix prefix (baseNameOf entry.path)) project.fetches
        );
      pyyaml = lib.sort (a: b: lib.head a < lib.head b) (systemsOf "pyyaml-");
    in
    assert lib.assertMsg (
      pyyaml == [
        [ "aarch64-darwin" ]
        [ "aarch64-linux" ]
        [ "x86_64-linux" ]
      ]
      && systemsOf "attrs-" == [ null ]
    ) "pip-purifier: fetches gives PyYAML the systems ${builtins.toJSON pyyaml}";
    runCommand "pip-purifier-test-fetches" { } "touch $out";

  # A project that adds a nixpkgs Python package as a native build input:
  # nixpkgs' Python setup hook puts it, and the Python environment of pip,
  # on PYTHONPATH, ahead of the locked packages. The environment build and
  # the sdist and wheel build clear PYTHONPATH before they run pip and
  # `python -m build`; a postBuild hook checks it in the same shell.
  pythonpath =
    let
      withSetuptools = build {
        src = ./fixtures/project;
        nativeBuildInputs = [ pkgs.python3Packages.setuptools ];
      };
      checked =
        drv:
        drv.overrideAttrs (old: {
          postBuild = (old.postBuild or "") + ''
            if [ -n "''${PYTHONPATH:-}" ]; then
              echo "PYTHONPATH reached pip: $PYTHONPATH" >&2
              exit 1
            fi
          '';
        });
    in
    pkgs.linkFarmFromDrvs "pip-purifier-test-pythonpath" [
      (checked withSetuptools.environment)
      (checked withSetuptools.packages.greet)
    ];

  conformance-offline =
    offline ./fixtures/project project.inputs "greet" "hello attrs=25.3.0 docopt=0.6.2 yaml=6.0.3"
      "sed -i 's/hello/howdy/' src/greet/__init__.py"
      "howdy attrs=25.3.0 docopt=0.6.2 yaml=6.0.3";

  # `nix build --rebuild` of this check, in CI, builds the sdist and wheel a
  # second time and compares them.
  conformance-rebuild = project.packages.greet;

  conformance-external = pkgs.linkFarmFromDrvs "pip-purifier-conformance-external" [
    (imports "external" external.environment "import idna; print(idna.__version__)" "3.10")
    (imports "external-pylock" externalPylock.environment
      "import six, packaging; print(six.__version__, packaging.__version__)"
      "1.17.0 25.0"
    )
  ];

  sources =
    runs "sources" sourcesReplayEnvironment
      "attrs=25.3.0 six=1.17.0 idna=3.10 iniconfig=2.1.0 pyparsing=3.2.3 localdir=0.3.0";

  pylock =
    imports "pylock" pylock.environment
      "import six, packaging; print(six.__version__, packaging.__version__)"
      "1.17.0 25.0";

  script = script.tests;

  inherit shims;

  # The unit tests of the lock tool, the input rewriter, the git rebuild and
  # the VCS shims (tests/unit).
  unit =
    runCommand "pip-purifier-test-unit"
      (
        {
          nativeBuildInputs = [
            (pkgs.python3.withPackages (ps: [
              ps.packaging
              ps.pip
            ]))
            pkgs.git
          ];
        }
        // loopback
      )
      ''
        export HOME=$TMPDIR
        ${pkgs.python3.interpreter} ${./network_check.py} > network.json
        export PIP_PURIFIER_NIX_LIB=${../.}/pkgs/build-pip-project
        export PIP_PURIFIER_PYTHON=${pkgs.python3.interpreter}
        export PIP_PURIFIER_NIXPKGS=${pkgs.path}
        export PYTHONPATH=${../pkgs/build-pip-project}:${../src}:${cli}/share/pip-purifier/purifier/tools
        python -m unittest discover -s ${./unit} -v
        mkdir -p $out
        cp network.json $out/network.json
      '';

  hashed = imports "hashed" hashed.environment "import idna; print(idna.__version__)" "3.10";

  cli = runCommand "pip-purifier-test-cli" { } ''
    ${lib.getExe cli} --help
    ${lib.getExe cli} lock --help
    ${lib.getExe cli} prefetch --help
    ${lib.getExe cli} pip --help > /dev/null
    ${lib.getExe cli} publish --help
    touch $out
  '';
  abi-locks = import ./abi-locks.nix {
    inherit pkgs;
    buildPipProject = build;
  };
  wheel-data = import ./wheel-data.nix {
    inherit pkgs;
    buildPipProject = build;
  };
}
// import ./conformance.nix {
  inherit pkgs;
  buildPipProject = build;
}
// lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
  native-runtime = import ./native-runtime.nix { inherit pkgs; };
  native-runtime-paths = import ./native-runtime-paths.nix { inherit pkgs; };
}
// lib.optionalAttrs (pkgs.stdenv.hostPlatform.system == "x86_64-linux") {
  native-runtime-node = nativeNode;
  native-runtime-node-fixup = nativeNode.fixup;
}
