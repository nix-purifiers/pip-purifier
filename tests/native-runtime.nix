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
    cat > program.cc <<'CPP'
    #include <iostream>
    #include <string>
    int main() { std::cout << std::string(42, 'x').size(); }
    CPP
    c++ -no-pie program.cc -o env/lib/python/site-packages/native_program
    patchelf --set-interpreter /missing/purifier-test-loader \
      --set-rpath '$ORIGIN/vendor' env/lib/python/site-packages/native_program
    printf 'void _start(void) { for (;;) {} }\n' > static.c
    cc -nostdlib -static -fno-stack-protector -Wl,-e,_start static.c \
      -o env/lib/python/site-packages/native_static
    cc -nostdlib -static-pie -fno-stack-protector -Wl,-e,_start static.c \
      -o env/lib/python/site-packages/native_static_pie
    python - <<'PY'
    import base64, csv, hashlib, json, os, pathlib, subprocess, sys
    root = pathlib.Path('env').resolve()
    site = root / 'lib/python/site-packages'
    library = site / 'native.so'
    program = site / 'native_program'
    static = site / 'native_static'
    static_pie = site / 'native_static_pie'
    foreign = site / 'foreign_program'
    content = bytearray(program.read_bytes())
    byteorder = 'little' if content[5] == 1 else 'big'
    machine = int.from_bytes(content[18:20], byteorder)
    content[18:20] = (183 if machine == 62 else 62).to_bytes(2, byteorder)
    foreign.write_bytes(content)
    originals = {path: path.read_bytes() for path in (static, static_pie, foreign)}
    record = site / 'native-1.0.dist-info/RECORD'
    record.write_text('native.so,old-hash,0\nnative_program,old-hash,0\nnative-1.0.dist-info/RECORD,,\n')
    command = [sys.executable, '-I', '-c',
               f'import ctypes; assert ctypes.CDLL({str(library)!r}).answer() == 100']
    before = subprocess.run(command, capture_output=True, text=True)
    assert before.returncode != 0 and any(
      name in before.stderr for name in ('libstdc++', 'libz.so')), before
    try:
      subprocess.run([str(program)], check=True)
    except FileNotFoundError:
      pass
    else:
      raise AssertionError('the bundled executable unexpectedly had a usable loader')
    pathlib.Path('inputs/native-runtime.json').write_text(json.dumps({
      'patchelf': '${pkgs.patchelf}/bin/patchelf',
      'dynamicLinker': '${pkgs.stdenv.cc.bintools.dynamicLinker}',
      'libraryPath': os.environ['RUNTIME_LIBRARY']}))
    from native_runtime import repair, refresh_records, elf_properties
    assert elf_properties(static_pie)['staticPie']
    repair('inputs', str(root))
    subprocess.run(command, check=True)
    assert subprocess.check_output([str(program)]) == b'42'
    assert all(path.read_bytes() == original for path, original in originals.items())
    assert '$ORIGIN/vendor' in subprocess.check_output([
      '${pkgs.patchelf}/bin/patchelf', '--print-rpath', str(library)], text=True)
    rows = list(csv.reader(record.open()))
    digest = base64.urlsafe_b64encode(hashlib.sha256(library.read_bytes()).digest()).rstrip(b'=').decode()
    assert rows[0] == ['native.so', 'sha256=' + digest, str(library.stat().st_size)], rows
    digest = base64.urlsafe_b64encode(hashlib.sha256(program.read_bytes()).digest()).rstrip(b'=').decode()
    assert rows[1] == ['native_program', 'sha256=' + digest, str(program.stat().st_size)], rows
    first = library.read_bytes()
    first_program = program.read_bytes()
    repair('inputs', str(root))
    assert library.read_bytes() == first, 'runtime repair must be idempotent'
    assert program.read_bytes() == first_program, 'executable repair must be idempotent'
    with program.open('ab') as output:
      output.write(b'post-fixup-test')
    refresh_records(str(root))
    rows = list(csv.reader(record.open()))
    digest = base64.urlsafe_b64encode(hashlib.sha256(program.read_bytes()).digest()).rstrip(b'=').decode()
    assert rows[1] == ['native_program', 'sha256=' + digest, str(program.stat().st_size)]
    assert rows[-1] == ["native-1.0.dist-info/RECORD", "", ""]
    PY
    touch $out
  ''
