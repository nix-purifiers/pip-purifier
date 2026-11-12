# SPDX-License-Identifier: MIT
"""Build-only constraint forwarding for one hash-certified management payload."""
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

IDENTITY = 'pip25-isolated-build-constraints-v1'
PAYLOAD = 'c18bfea71aaad05db171ceacedd48efaa5f1045cf323cd24565348afa5e01a57'
BUILD_ENV = '0efe14082952838b8d69a97f84bfadae0e7ece5dc8b3d0ae203c640a10a45c5e'
STATE = 'PIP_PURIFIER_BUILD_CONSTRAINTS'


def available():
    from pip._internal import build_env
    if os.environ.get('PIP_PURIFIER_DRIVER_SHA256') != PAYLOAD:
        return False
    if hashlib.sha256(Path(build_env.__file__).read_bytes()).hexdigest() != BUILD_ENV:
        return False
    try:
        inspect.signature(build_env.call_subprocess).bind([], command_desc='probe')
        inspect.signature(build_env.BuildEnvironment._install_requirements).bind(None, None, [], None, kind='probe')
    except (AttributeError, TypeError, ValueError):
        return False
    return True


def forward_child(args, constraints, runner, native_entry):
    args = list(args)
    boundary = args.index('--') if '--' in args else -1
    flags = args[:boundary] if boundary >= 0 else []
    target = flags.index('--target') if '--target' in flags else -1
    if (len(args) < 3 or args[0] != sys.executable or args[1] != native_entry
            or args[2] != 'install' or '--prefix' not in flags
            or '--ignore-installed' not in flags or target < 0
            or target + 1 >= len(flags) or flags[target + 1] != ''):
        raise SystemExit('pip-purifier: unsupported isolated build child protocol')
    additions = []
    for value in constraints:
        additions += ['--constraint', value, '--build-constraint', value]
    args[1] = str(runner)
    args[boundary:boundary] = additions
    return args


def activate(runner):
    from pip._internal.commands import create_command
    from pip._internal.cli.main_parser import create_main_parser
    from pip._internal import build_env
    from pip._internal.network.session import PipSession
    from pip._internal.req.req_file import get_file_content

    constraints = json.loads(os.environ.get(STATE, '[]'))
    args = sys.argv[1:]
    cleaned = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == '--':
            cleaned.extend(args[index:])
            break
        if arg == '--build-constraint':
            index += 1
            if index == len(args):
                raise SystemExit('pip-purifier: --build-constraint requires a value')
            constraints.append(args[index])
        elif arg.startswith('--build-constraint='):
            constraints.append(arg.partition('=')[2])
        else:
            cleaned.append(arg)
        index += 1
    global_options, command_args = create_main_parser().parse_args(cleaned)
    # This configuration belongs only to build-capable commands. Native
    # --isolated suppresses external configuration, not forwarded tool state.
    if global_options.version or not command_args or command_args[0] not in ('install', 'wheel', 'download'):
        return
    if not global_options.isolated_mode:
        constraints.extend(os.environ.get('PIP_BUILD_CONSTRAINT', '').split())
    if not constraints:
        return
    constraints = list(dict.fromkeys(value if urlsplit(value).scheme in ('http', 'https', 'file') else str(Path(value).absolute())
                                    for value in constraints))
    command = create_command(command_args[0], isolated=global_options.isolated_mode)
    options, _ = command.parse_args(command_args[1:])
    if not getattr(options, 'build_isolation', True):
        raise SystemExit('--build-constraint cannot be used with --no-build-isolation.')
    with PipSession() as session:
        for value in constraints:
            get_file_content(value, session)
    # Native delegation retains tool state; no runtime constraint variable
    # is changed. Every isolated dependency child receives both scopes, as
    # native pip does, so nested source builds retain build constraints.
    os.environ[STATE] = json.dumps(constraints)
    sys.argv[1:] = cleaned
    original = build_env.call_subprocess

    def build_child(args, *positional, **keywords):
        forwarded = forward_child(args, constraints, runner, build_env.get_runnable_pip())
        return original(forwarded, *positional, **keywords)

    build_env.call_subprocess = build_child
