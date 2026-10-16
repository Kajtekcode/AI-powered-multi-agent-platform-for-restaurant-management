#!/usr/bin/env python3
"""
Run the Restaurant Growth OS server.
Usage:
  python run.py                  # development mode
  python run.py --prod           # production mode
  python run.py --port 8080      # custom port
"""
import argparse
import logging
import os
import sys

import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Restaurant Growth OS")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.getenv("APP_PORT", 8000)))
    parser.add_argument("--prod", action="store_true")
    parser.add_argument("--reload", action="store_true", default=True)
    args = parser.parse_args()

    is_prod = args.prod or os.getenv("APP_ENV") == "production"

    print(f"""
╔══════════════════════════════════════════════╗
║       Restaurant Growth OS  v2.0             ║
║  Dashboard → http://localhost:{args.port}          ║
║  API Docs  → http://localhost:{args.port}/api/docs  ║
╚══════════════════════════════════════════════╝
""")

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=not is_prod,
        log_level="info" if is_prod else "debug",
        access_log=True,
    )
