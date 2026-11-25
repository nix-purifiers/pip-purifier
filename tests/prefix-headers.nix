{ pkgs }:
let
  python = pkgs.python313;
  pip = python.withPackages (ps: [ ps.pip ]);
in
pkgs.runCommand "pip-purifier-prefix-header-install" {
  nativeBuildInputs = [ pkgs.stdenv.cc ];
} ''
  unset PYTHONPATH PYTHONHOME NIX_PYTHONPATH
  ${pkgs.python3.interpreter} -I ${./prefix-headers.py} \
    --python ${python.interpreter} --pip ${pip.interpreter} \
    --cc ${pkgs.stdenv.cc}/bin/cc --bash ${pkgs.bash}/bin/bash \
    --script ${../pkgs/build-pip-project/make-prefix.sh} \
    --original-script ${./fixtures/prefix-headers/original-make-prefix.sh} \
    --out "$out"
''
