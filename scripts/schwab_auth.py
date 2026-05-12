#!/usr/bin/env python3
"""One-time Schwab Developer API OAuth setup.

You only need to run this once (and then again every ~7 days if the refresh
token expires without being rotated). It saves a token file that the
ShivaGPT server uses to talk to Schwab on your behalf, READ-ONLY.

Prerequisite — register an app at https://developer.schwab.com:

  1. Create a personal account at developer.schwab.com.
  2. Click "Dashboard" → "API Products" and *enable* both:
       - Accounts and Trading Production
       - Market Data Production
     (Both are free; "Trading" is the name of the product, not what we use.)
  3. Click "Add a New App" and fill in:
       - App Name:    anything, e.g. "shivagpt-personal"
       - Callback URL: https://127.0.0.1
       - API Product: select both products from step 2
     Schwab reviews new apps; "Approved" usually arrives within a day,
     sometimes hours. You can't run this script until your app is approved.
  4. Once approved, open the app and copy "App Key" + "App Secret".

Usage:

  # From your Mac (browser available) is easiest:
  export SCHWAB_APP_KEY=your_key
  export SCHWAB_APP_SECRET=your_secret
  ./scripts/schwab_auth.py --token-path data/schwab-token.json

  # Then copy the token file to kailash:
  scp data/schwab-token.json kailash:~/shivagpt/data/schwab-token.json

  # OR run directly on kailash with the --manual flag (no browser needed):
  ssh kailash 'cd ~/shivagpt && \
    SCHWAB_APP_KEY=... SCHWAB_APP_SECRET=... \
    .venv/bin/python scripts/schwab_auth.py --manual'

After the token is in place, restart ShivaGPT and try /tp in the UI.
"""

from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-time Schwab Developer API OAuth setup.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--token-path",
        default=os.getenv("SCHWAB_TOKEN_PATH", "data/schwab-token.json"),
        help="Where to save the token file (default: %(default)s)",
    )
    parser.add_argument(
        "--callback-url",
        default="https://127.0.0.1",
        help="The exact callback URL you registered at developer.schwab.com "
             "(default: %(default)s)",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Manual flow: prints the auth URL and asks you to paste the "
             "redirected URL back. Use this on headless servers (no browser).",
    )
    args = parser.parse_args()

    api_key = (os.getenv("SCHWAB_APP_KEY") or os.getenv("SCHWAB_API_KEY") or "").strip()
    app_secret = (os.getenv("SCHWAB_APP_SECRET") or os.getenv("SCHWAB_API_SECRET") or "").strip()
    if not api_key or not app_secret:
        print("ERROR: SCHWAB_APP_KEY and SCHWAB_APP_SECRET environment variables must be set.")
        print("Export them in your shell, or pass them via a wrapper command.")
        return 1

    try:
        from schwab.auth import client_from_manual_flow, client_from_login_flow
    except ImportError:
        print("ERROR: schwab-py is not installed. Run:")
        print("  pip install schwab-py")
        return 1

    token_path = Path(args.token_path).expanduser().resolve()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Token file will be saved to: {token_path}")
    print(f"Callback URL must match the one registered at developer.schwab.com:")
    print(f"  {args.callback_url}")
    print()

    if args.manual:
        print("=== Manual flow ===")
        print("1. The script will print a URL.")
        print("2. Open it in any browser, log in to your Schwab account, allow access.")
        print("3. Schwab redirects to your callback URL — the browser will likely")
        print(f"   show 'Can't connect to {args.callback_url}'. That's fine.")
        print("4. Copy the *full* URL from the browser's address bar (it has a")
        print("   'code=...' query parameter) and paste it back here when prompted.")
        print()
        client = client_from_manual_flow(
            api_key=api_key,
            app_secret=app_secret,
            callback_url=args.callback_url,
            token_path=str(token_path),
        )
    else:
        print("=== Login flow (opens a browser locally) ===")
        print("schwab-py will spawn a local HTTPS server, open your browser to the")
        print("Schwab auth page, then capture the redirect automatically.")
        print()
        client = client_from_login_flow(
            api_key=api_key,
            app_secret=app_secret,
            callback_url=args.callback_url,
            token_path=str(token_path),
        )

    # Smoke-test: list account numbers.
    print()
    print("Testing the connection…")
    resp = client.get_account_numbers()
    if resp.status_code != 200:
        print(f"FAIL: get_account_numbers() returned {resp.status_code}")
        print(resp.text[:500])
        return 2
    accounts = resp.json() or []
    print(f"OK — Schwab returned {len(accounts)} account(s).")
    for a in accounts:
        num = a.get("accountNumber") or "?"
        print(f"  - account {num[-4:].rjust(len(num), '*')}")
    print()
    print(f"Token saved to {token_path}.")
    print("If you ran this on a different host, copy the file to kailash next:")
    print(f"  scp {token_path} kailash:~/shivagpt/data/schwab-token.json")
    print("Then restart ShivaGPT and try /tp in the UI.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
