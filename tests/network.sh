# SPDX-License-Identifier: MIT
# Source this before changing directories. Fail closed when isolation is absent.
offline_network() {
  case "$(uname -s)" in
    Linux)
      if unshare -rn true 2>/dev/null; then
        unshare -rn env NIX_REMOTE=daemon "$@"
      elif sudo -n unshare -n true 2>/dev/null; then
        sudo -n unshare -n sudo -n -u "$(id -un)" \
          env "PATH=$PATH" "HOME=$HOME" NIX_REMOTE=daemon "$@"
      else
        echo 'cannot enforce offline networking: network namespace unavailable' >&2
        return 1
      fi
      ;;
    Darwin)
      # Deny IP connections, including direct sockets and proxy bypasses.
      # Unix-domain sockets remain available for the Nix daemon.
      /usr/bin/sandbox-exec -p \
        '(version 1)(allow default)(deny network* (remote ip "*:*"))' "$@"
      ;;
    *) echo 'no enforced offline network isolation for this OS' >&2; return 1 ;;
  esac
}
