import os

from dotenv import load_dotenv

from bot import main


if __name__ == "__main__":
    # Prefer `.env`. If it doesn't exist, fall back to `.env.example`.
    # This lets you run immediately after filling in your example keys.
    env_path = ".env" if os.path.exists(".env") else ".env.example"
    # Do not overwrite already-set environment variables.
    # Force-load values from the env file so regenerated keys are used.
    load_dotenv(dotenv_path=env_path, override=True)
    missing = [k for k in ["ALPACA_API_KEY", "ALPACA_API_SECRET"] if not os.getenv(k)]
    if missing:
        raise SystemExit(f"Missing env vars: {missing}. Check {env_path} contains your Alpaca keys.")
    main()

