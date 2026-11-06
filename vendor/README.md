# Bundled HTML parsers

Official PyPI pure-Python wheels, unmodified, downloaded with:

```sh
python -m pip download --no-deps --only-binary=:all: beautifulsoup4==4.12.3 soupsieve==2.5
```

- Beautiful Soup 4.12.3: https://pypi.org/project/beautifulsoup4/4.12.3/
  - SHA256: `b80878c9f40111313e55da8ba20bdba06d8fa3969fc68304167741bbf9e082ed`
  - MIT license included at `beautifulsoup4-4.12.3.dist-info/licenses/LICENSE` in the wheel.
- Soup Sieve 2.5: https://pypi.org/project/soupsieve/2.5/
  - SHA256: `eaa337ff55a1579b6549dc679565eac1e3d000563bcb1c8ab0d0fefbc0c2cdc7`
  - MIT license included at `soupsieve-2.5.dist-info/licenses/LICENSE.md` in the wheel.

Requires Python 3.8+. Python reads these ZIP-format wheels directly; no extraction, pip subprocess, or network access is performed at startup. Existing installed or imported packages take precedence. Only a missing package's wheel is appended to the Python import path. To update, replace the pinned wheel, update `parser_dependencies.py` and the integrity test, and run the clean-environment tests.
