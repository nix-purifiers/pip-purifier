# Node's official executable reproduces a patchelf program-header sorting crash.
# Keep the artifact pinned: the regression exercises real loader behavior.
{ pkgs, environment }:
let
  archive = pkgs.fetchurl {
    url = "https://nodejs.org/dist/v24.22.1/node-v24.22.1-linux-x64.tar.xz";
    sha256 = "6c172a9ab6718f7b801fb5a7bd486af2ee5e32758086a04b84fcce2aa77095e6";
  };
in
pkgs.runCommand "pip-purifier-native-runtime-node"
  {
    nativeBuildInputs = [
      pkgs.python3
      pkgs.gnutar
      pkgs.xz
    ];
    passthru.fixup = environment.overrideAttrs (old: {
      name = "pip-purifier-native-node-environment-fixup";
      nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [
        pkgs.gnutar
        pkgs.xz
      ];
      postBuild = (old.postBuild or "") + ''
        tar -xJf ${archive} --strip-components=2 -C "$out/bin" node-v24.22.1-linux-x64/bin/node
        export PYTHONPATH=${../pkgs/build-pip-project}
        ${pkgs.python3.interpreter} - <<'PY'
        import os, pathlib
        from native_runtime import repair
        root = pathlib.Path(os.environ['out'])
        record = root / '${pkgs.python3.sitePackages}/node-24.22.1.dist-info/RECORD'
        record.parent.mkdir()
        record.write_text('../../../bin/node,old-hash,0\nnode-24.22.1.dist-info/RECORD,,\n')
        repair('${environment.inputs}', root)
        PY
      '';
      postFixup = (old.postFixup or "") + ''
        "$out/bin/node" -e 'require("node:child_process").execFileSync(process.execPath, ["--version"], {stdio:"inherit"})'
        ${pkgs.python3.interpreter} - <<'PY'
        import base64, csv, hashlib, os, pathlib
        root = pathlib.Path(os.environ['out'])
        node = root / 'bin/node'
        rows = list(csv.reader((root / '${pkgs.python3.sitePackages}/node-24.22.1.dist-info/RECORD').open()))
        digest = base64.urlsafe_b64encode(hashlib.sha256(node.read_bytes()).digest()).rstrip(b'=').decode()
        assert rows[0] == ['../../../bin/node', 'sha256=' + digest, str(node.stat().st_size)]
        PY
      '';
    });
  }
  ''
    export PYTHONPATH=${../pkgs/build-pip-project}
    unset LD_LIBRARY_PATH
    mkdir -p env/lib/python/site-packages/node-24.22.1.dist-info inputs
    tar -xJf ${archive} --strip-components=2 -C env/lib/python/site-packages node-v24.22.1-linux-x64/bin/node
    python - <<'PY'
    import base64, csv, hashlib, json, pathlib, subprocess
    from native_runtime import repair, refresh_records
    root = pathlib.Path('env').resolve()
    node = root / 'lib/python/site-packages/node'
    record = root / 'lib/python/site-packages/node-24.22.1.dist-info/RECORD'
    record.write_text('node,old-hash,0\nnode-24.22.1.dist-info/RECORD,,\n')
    settings = {
      'patchelf': '${pkgs.patchelf}/bin/patchelf',
      'dynamicLinker': '${pkgs.stdenv.cc.bintools.dynamicLinker}',
      'libraryPath': '${
        pkgs.lib.makeLibraryPath [
          pkgs.stdenv.cc.cc.lib
          pkgs.zlib
        ]
      }',
    }
    # Establish that the original binary works with the same loader/libraries.
    assert subprocess.check_output([
      settings['dynamicLinker'], '--library-path', settings['libraryPath'],
      str(node), '--version']) == b'v24.22.1\n'
    pathlib.Path('inputs/native-runtime.json').write_text(json.dumps(settings))
    repair('inputs', root)
    assert subprocess.check_output([str(node), '--version']) == b'v24.22.1\n'
    # Child execution must work too, without a shell environment workaround.
    script = 'require("node:child_process").execFileSync(process.execPath, ["--version"], {stdio:"inherit"})'
    assert subprocess.check_output([str(node), '-e', script]) == b'v24.22.1\n'
    first = hashlib.sha256(node.read_bytes()).hexdigest()
    repair('inputs', root)
    assert hashlib.sha256(node.read_bytes()).hexdigest() == first
    # Environment derivations shrink RPATHs and then refresh RECORD, rather
    # than adding unused runtime paths back after shrinking removed them.
    subprocess.run([settings['patchelf'], '--no-sort', '--shrink-rpath', str(node)], check=True)
    assert subprocess.check_output([str(node), '-e', script]) == b'v24.22.1\n'
    refresh_records(root)
    rows = list(csv.reader(record.open()))
    digest = base64.urlsafe_b64encode(hashlib.sha256(node.read_bytes()).digest()).rstrip(b'=').decode()
    assert rows[0] == ['node', 'sha256=' + digest, str(node.stat().st_size)]
    PY
    touch "$out"
  ''
