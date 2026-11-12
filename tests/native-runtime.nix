# A manylinux-style C++ library has no Nix runtime search paths. Installation
# must make it loadable without shell activation or LD_LIBRARY_PATH.
{ pkgs }:
pkgs.runCommand "pip-purifier-native-runtime"
  {
    nativeBuildInputs = [
      pkgs.stdenv.cc
      pkgs.patchelf
      pkgs.python3
    ];
    buildInputs = [ pkgs.zlib ];
  }
  ''
    export PYTHONPATH=${../pkgs/build-pip-project}
    export RUNTIME_LIBRARY=${
      pkgs.lib.makeLibraryPath [
        pkgs.stdenv.cc.cc.lib
        pkgs.zlib
      ]
    }
    unset LD_LIBRARY_PATH
    cat > native.cc <<'CPP'
    #include <string>
    #include <zlib.h>
    extern "C" unsigned int answer() {
      std::string value(100, 'x');
      return value.size() + (zlibVersion()[0] == '1' ? 0 : 1);
    }
    CPP
    mkdir -p env/lib/python/site-packages/native-1.0.dist-info inputs
    c++ -shared -fPIC native.cc -lz -o env/lib/python/site-packages/native.so
    patchelf --set-rpath '$ORIGIN/vendor' env/lib/python/site-packages/native.so
    python - <<'PY'
    import base64, csv, hashlib, json, os, pathlib, subprocess, sys
    root = pathlib.Path('env').resolve()
    site = root / 'lib/python/site-packages'
    library = site / 'native.so'
    record = site / 'native-1.0.dist-info/RECORD'
    record.write_text('native.so,old-hash,0\nnative-1.0.dist-info/RECORD,,\n')
    command = [sys.executable, '-I', '-c',
               f'import ctypes; assert ctypes.CDLL({str(library)!r}).answer() == 100']
    before = subprocess.run(command, capture_output=True, text=True)
    assert before.returncode != 0 and any(
      name in before.stderr for name in ('libstdc++', 'libz.so')), before
    pathlib.Path('inputs/native-runtime.json').write_text(json.dumps({
      'patchelf': '${pkgs.patchelf}/bin/patchelf',
      'libraryPath': os.environ['RUNTIME_LIBRARY']}))
    from native_runtime import repair
    repair('inputs', str(root))
    subprocess.run(command, check=True)
    assert '$ORIGIN/vendor' in subprocess.check_output([
      '${pkgs.patchelf}/bin/patchelf', '--print-rpath', str(library)], text=True)
    rows = list(csv.reader(record.open()))
    digest = base64.urlsafe_b64encode(hashlib.sha256(library.read_bytes()).digest()).rstrip(b'=').decode()
    assert rows[0] == ['native.so', 'sha256=' + digest, str(library.stat().st_size)], rows
    first = library.read_bytes()
    repair('inputs', str(root))
    assert library.read_bytes() == first, 'runtime repair must be idempotent'
    PY
    touch $out
  ''
