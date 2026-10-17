"""Entry point for running the Livedoc server as a module."""

import sys
sys.path.insert(0, "/home/user/work")

from server import main
import asyncio

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", type=str, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.port, args.data_dir))
