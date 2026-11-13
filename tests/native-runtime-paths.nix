# Preserve transitive vendor RPATH semantics through repair and Nix shrinking.
{ pkgs }:
pkgs.runCommand "pip-purifier-native-runtime-paths"
  {
    nativeBuildInputs = [
      pkgs.stdenv.cc
      pkgs.patchelf
      pkgs.python3
    ];
  }
  ''
    export PYTHONPATH=${../pkgs/build-pip-project}
    unset LD_LIBRARY_PATH
    mkdir -p env/lib/python/site-packages/vendor inputs
    site=$PWD/env/lib/python/site-packages
    printf 'int leaf(void) { return 42; }\n' > leaf.c
    printf 'extern int leaf(void); int child(void) { return leaf(); }\n' > child.c
    printf 'extern int child(void); int answer(void) { return child(); }\n' > anchor.c
    cc -shared -fPIC leaf.c -Wl,-soname,libpurifierleaf.so -o "$site/vendor/libpurifierleaf.so"
    cc -shared -fPIC child.c -L"$site/vendor" -lpurifierleaf \
      -Wl,-soname,libpurifierchild.so -o "$site/vendor/libpurifierchild.so"
    cc -shared -fPIC anchor.c -L"$site/vendor" -lpurifierchild -o "$site/anchor.so"
    patchelf --remove-rpath "$site/vendor/libpurifierleaf.so"
    patchelf --remove-rpath "$site/vendor/libpurifierchild.so"
    patchelf --force-rpath --set-rpath '$ORIGIN/vendor' "$site/anchor.so"
    cp "$site/anchor.so" "$site/runpath.so"
    patchelf --set-rpath '$ORIGIN/vendor' "$site/runpath.so"
    cp "$site/vendor/libpurifierleaf.so" "$site/empty-runpath.so"
    patchelf --set-rpath /missing/purifier-test-library "$site/empty-runpath.so"
    patchelf --shrink-rpath "$site/empty-runpath.so"
    python - <<'PY'
    import json, pathlib, subprocess, sys
    from native_runtime import repair, elf_properties
    root = pathlib.Path('env').resolve()
    site = root / 'lib/python/site-packages'
    tool = '${pkgs.patchelf}/bin/patchelf'
    linker = '${pkgs.stdenv.cc.bintools.dynamicLinker}'
    proxy = [sys.executable, '-I', '${../pkgs/build-pip-project/native_runtime.py}',
             '--patchelf', tool, linker, '--']
    def load(name):
        return subprocess.run([sys.executable, '-I', '-c',
            f'import ctypes; assert ctypes.CDLL({str(site / name)!r}).answer() == 42'],
            capture_output=True, text=True)
    assert load('anchor.so').returncode == 0
    assert load('runpath.so').returncode != 0
    leaf = site / 'vendor/libpurifierleaf.so'
    subprocess.run([*proxy, '--shrink-rpath', str(leaf)], check=True)
    assert elf_properties(leaf)['pathKind'] is None
    assert elf_properties(site / 'empty-runpath.so')['pathKind'] == 'runpath'
    pathlib.Path('inputs/native-runtime.json').write_text(json.dumps({
        'patchelf': tool, 'dynamicLinker': linker,
        'libraryPath': '${pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ]}'}))
    repair('inputs', root)
    for path in site.rglob('*.so'):
        subprocess.run([*proxy, '--shrink-rpath', str(path)], check=True)
    assert elf_properties(site / 'anchor.so')['pathKind'] == 'rpath'
    assert elf_properties(site / 'runpath.so')['pathKind'] == 'runpath'
    assert elf_properties(site / 'empty-runpath.so')['pathKind'] == 'runpath'
    positive = load('anchor.so')
    assert positive.returncode == 0, positive.stderr
    negative = load('runpath.so')
    assert negative.returncode != 0 and 'libpurifierleaf.so' in negative.stderr, negative.stderr
    PY
    touch $out
  ''
