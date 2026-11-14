# SPDX-License-Identifier: MIT
"""Run the native sdist backend in an isolated, runtime-repaired environment."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import build
import isolated_native
import offline


def main(python, directory, destination):
    env = dict(os.environ)
    directory, destination = str(Path(directory).resolve()), str(Path(destination).resolve())

    def runner(command, cwd=None, extra_environ=None):
        subprocess.run(command, cwd=cwd, env={**env, **(extra_environ or {})}, check=True)

    with tempfile.TemporaryDirectory(prefix='pip-purifier-sdist-') as temporary:
        venv = Path(temporary) / 'venv'
        subprocess.run([python, '-m', 'venv', '--without-pip', str(venv)], env=env, check=True)

        class Environment:
            python_executable = str(venv / 'bin/python')

            def make_extra_environ(self):
                return {'PATH': str(venv / 'bin') + os.pathsep + env.get('PATH', '')}

            def install(self, requirements):
                if requirements:
                    offline.run_pip([python, '-m', 'pip', '--python', self.python_executable,
                                     'install', *sorted(requirements)], directory, env)
                    isolated_native.repair(venv)

        environment = Environment()
        builder = build.ProjectBuilder.from_isolated_env(environment, directory, runner=runner)
        environment.install(builder.build_system_requires)
        environment.install(builder.get_requires_for_build('sdist'))
        return builder.build('sdist', destination)


if __name__ == '__main__':
    main(*sys.argv[1:])
