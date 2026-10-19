import argparse
import json
import urllib.request

from .core import db, get_state, now


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['health','backup'])
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--destination')
    args = parser.parse_args()
    if args.command == 'backup':
        from .backup import export
        if not args.destination:
            parser.error('--destination is required')
        print(json.dumps(export(args.destination)))
    elif args.worker:
        with db() as c:
            if now() - get_state(c, 'worker_at', 0) > 45:
                raise SystemExit(1)
    else:
        with urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4) as response:
            if not json.load(response)['ok']:
                raise SystemExit(1)


if __name__ == '__main__':
    main()
