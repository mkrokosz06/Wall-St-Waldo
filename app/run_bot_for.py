import os
import sys
from dotenv import load_dotenv

from bot import main


def _pick_env_path() -> str:
    return ".env" if os.path.exists(".env") else ".env.example"


if __name__ == "__main__":
    duration_sec = 1800
    if len(sys.argv) >= 2:
        duration_sec = int(sys.argv[1])

    # Make the bot stop after the requested runtime.
    os.environ["BOT_MAX_RUNTIME_SEC"] = str(duration_sec)

    env_path = _pick_env_path()
    load_dotenv(dotenv_path=env_path, override=True)

    main()

