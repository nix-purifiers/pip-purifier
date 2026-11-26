# SPDX-License-Identifier: MIT
#
# `pypi-handler`: the HTTP handler of Python package indexes
# (pypi_handler.py), for a purifier whose lock run records a pip install
# made by the build itself. It reads the lock run's HTTP records on
# standard input and prints the URLs it claims and the entries that
# replace them, as tools/http_records.py of the purifier library has it.
# A project gives it to such a purifier through that purifier's scope:
#
#   cmake-purifier.overrideScope (_: _: {
#     httpHandlers = [ pip-purifier.pypi-handler ];
#   })
{
  lib,
  writeShellApplication,
  python3,
}:
let
  # The purifier library at the revision this flake's lock pins, for
  # tools/http_records.py.
  flakeLock = builtins.fromJSON (builtins.readFile ../../flake.lock);
  purifierSource = builtins.fetchTree flakeLock.nodes.${flakeLock.nodes.root.inputs.purifier}.locked;
  python = python3.withPackages (ps: [ ps.packaging ]);
in
writeShellApplication {
  name = "pypi-handler";
  runtimeInputs = [ python ];
  text = ''
    PYTHONPATH=${purifierSource}/tools exec python3 ${./pypi_handler.py}
  '';
  meta.description = "The HTTP handler of Python package indexes for purifier lock runs";
}
