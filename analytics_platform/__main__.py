"""`python -m analytics_platform <demo|serve[:port]|ask ...>` entrypoint."""
import sys

from .cli import main as cli_main


def main() -> int:
    args = sys.argv[1:]
    if not args:
        cli_main(["demo"])
        return 0
    if args[0] == "serve":
        port = 8000
        if len(args) > 1 and args[1].isdigit():
            port = int(args[1])
        from .serve import make_serve
        import uvicorn
        app = make_serve()
        uvicorn.run(app, host="0.0.0.0", port=port)
        return 0
    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())