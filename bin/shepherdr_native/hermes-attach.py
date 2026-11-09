"""Run gateway attachment on the interpreter/import path selected by Hermes."""
import asyncio
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import sys
import site

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _native_imports(target):
    """Use the owner's pinned dependencies; cold calls only read PM selection.

    Importing hermes_bootstrap here is deliberately forbidden: its import may
    provision/recover the installation. The PM selection helpers below never do.
    """
    source = Path(target['source']).resolve()
    pinned = target.get('pythonPath')
    if pinned:
        paths = [Path(value) for value in pinned.split(os.pathsep) if value]
        packages = [path for path in paths if path.name in {'site-packages', 'dist-packages'}]
    else:
        try:
            from pm.environments import committed_venv, site_packages
        except ModuleNotFoundError as error:
            if error.name not in {'pm', 'pm.environments'}:
                raise
            packages = []  # Pre-PM Hermes uses its own interpreter's installed packages.
        else:
            selected = committed_venv(source)
            packages = [site_packages(selected)] if selected is not None else []
    for package in packages:
        if not package.is_dir():
            raise RuntimeError('Hermes owning dependency environment is no longer available; no install or fallback was attempted')
        site.addsitedir(str(package))
    # Native source and the pinned dependency tree take precedence over a base
    # interpreter's packages, exactly as Hermes' own dependency activation does.
    preferred = [str(source), *(str(package) for package in packages)]
    sys.path[:] = [*preferred, *(value for value in sys.path if value not in preferred)]


def main():
    request = json.load(sys.stdin)
    try:
        # No CLI main import (it pre-parses argv and can silently change profiles).
        # Native gateway modules are loaded only after HERMES_HOME is pinned.
        from shepherdr_native.hermes import _execute
        with redirect_stdout(sys.stderr):
            _native_imports(request['target'])
            result = asyncio.run(_execute(request['operation'], request['payload'], request['target']))
        response = {'ok': True, 'data': result}
        code = 0
    except ImportError as error:
        missing = getattr(error, 'name', None) or 'native module'
        response = {'ok': False, 'error': f'Hermes native dependency or attachment API is unavailable ({missing}); use the owning launcher environment (no install or stdio fallback)'}
        code = 1
    except Exception as error:
        # Native RPC errors contain the owner's bounded reason/detail. Never emit
        # the subprocess environment, ticket, endpoint URL, traceback or stderr.
        response = {'ok': False, 'error': str(error) or type(error).__name__}
        code = 1
    print(json.dumps(response))
    return code


if __name__ == '__main__':
    sys.exit(main())
