# SPDX-License-Identifier: MIT
#
# make-prefix.sh <python> <lib prefix> <out>
#
# Makes <out> a separate Python installation for the interpreter
# <python>: a copy of the interpreter in bin/, the standard library and
# the headers linked from <python>, and a site-packages directory that the
# environment's packages are installed into. CPython finds its prefix
# from the directory of the executable, so sys.prefix and sys.base_prefix
# are both <out>, and a virtual environment made from it with
# --system-site-packages sees the packages of <out>. A virtual environment
# made from another one would see only those of <python>.
set -euo pipefail
python=$1
libPrefix=$2
out=$3

mkdir -p "$out/bin" "$out/lib/$libPrefix/site-packages"
version=${libPrefix#python}
# A copy, rather than a link: CPython looks for its standard library
# relative to the real path of its executable.
cp "$python/bin/python$version" "$out/bin/python$version"
chmod u+w "$out/bin/python$version"
ln -s "python$version" "$out/bin/python3"
ln -s "python$version" "$out/bin/python"

# EXTERNALLY-MANAGED (PEP 668) stops pip from installing into the
# interpreter of nixpkgs; the packages of <out> are installed by pip.
for entry in "$python/lib/$libPrefix"/*; do
  name=${entry##*/}
  case "$name" in
    site-packages | EXTERNALLY-MANAGED) ;;
    *) ln -s "$entry" "$out/lib/$libPrefix/$name" ;;
  esac
done
# nixpkgs keeps sitecustomize.py, which reads NIX_PYTHONPATH, and the
# sysconfig data in the site-packages of the interpreter.
for entry in "$python/lib/$libPrefix/site-packages"/*; do
  ln -s "$entry" "$out/lib/$libPrefix/site-packages/${entry##*/}"
done
for entry in "$python/lib"/*; do
  name=${entry##*/}
  [ "$name" = "$libPrefix" ] || ln -s "$entry" "$out/lib/$name"
done
ln -s "$python/include" "$out/include"
