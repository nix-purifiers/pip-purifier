# SPDX-License-Identifier: MIT
"""Generate native pip resolver bindings from Nix-selected Python wheelhouses."""
import argparse
import json
from pathlib import Path
import sys


def target_tags(value):
    from packaging.tags import parse_tag
    if not isinstance(value, list) or not value:
        raise ValueError('consumer Python tags must be a nonempty ordered list')
    for tag in value:
        if not isinstance(tag, str) or len(parse_tag(tag)) != 1 or str(next(iter(parse_tag(tag)))) != tag:
            raise ValueError('invalid consumer Python tag: ' + repr(tag))
    if len(set(value)) != len(value):
        raise ValueError('duplicate consumer Python tags')
    return value


def write(views, old_roots, destination, python=None, compatible_tags=None):
    from python_repository import interpreter_tags, selections
    if python is not None and compatible_tags is not None:
        raise ValueError('consumer Python and tags are mutually exclusive')
    if python is not None:
        tags = target_tags(interpreter_tags(python))
    elif compatible_tags is not None:
        tags = target_tags(compatible_tags)
    elif views:
        raise ValueError('supplied Python inputs require consumer target tags or an explicit Python')
    else:
        tags = []
    selected = selections(views, tags)
    destination.mkdir(parents=True, exist_ok=True)
    overrides = destination / 'requirements.txt'
    overrides.write_text(''.join(name + ' @ ' + module['wheel'].as_uri() + '\n'
                                 for name, module in sorted(selected.items())))
    configuration = {
        'modules': {name: {'version': module['version'], 'wheel': str(module['wheel'])}
                    for name, module in selected.items()},
        'oldRepositories': list(dict.fromkeys([*old_roots, *(str(view['repository']) for view in views)])),
        'overrides': str(overrides),
    }
    filename = destination / 'inputs.json'
    filename.write_text(json.dumps(configuration, sort_keys=True) + '\n')
    return filename


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--views', type=Path, required=True)
    parser.add_argument('--old-repositories', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--tools', required=True)
    target = parser.add_mutually_exclusive_group()
    target.add_argument('--python')
    target.add_argument('--tags', type=Path)
    args = parser.parse_args()
    sys.path.insert(0, args.tools)
    write(json.loads(args.views.read_text()), json.loads(args.old_repositories.read_text()), args.destination,
          python=args.python, compatible_tags=json.loads(args.tags.read_text()) if args.tags is not None else None)


if __name__ == '__main__':
    main()
