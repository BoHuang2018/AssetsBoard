# Tests never read the real project .env (private addresses / ids): point the loader at a path that doesn't exist.
import os as _os

_os.environ["ASSETSBOARD_ENV_FILE"] = _os.path.join(_os.path.dirname(__file__), "_no_such_env_file")
