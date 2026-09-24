"""Explicit private output; no argument or exception echo to public streams."""
import argparse
from . import ViewError, create_view


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ViewError('CLI_USAGE')


def main(argv=None):
    try:
        parser = _Parser(add_help=False, allow_abbrev=False)
        for name in ('store', 'store-id', 'batch-id', 'output'):
            parser.add_argument('--' + name, required=True)
        args = parser.parse_args(argv)
        result = create_view(args.store, args.store_id, args.batch_id, args.output)
        code = 0
    except Exception as error:
        if not isinstance(error, ViewError):
            error = ViewError('INTERNAL_ERROR')
        result, code = error.failure_bytes, 2
    print(result.decode('ascii'))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
