"""Privacy-safe explicit CLI: one acknowledgement, never echoed arguments."""
import sys
from . import create_backup, verify_backup, recover_backup, check_recovery, BackupError
from .contracts import acknowledgement

def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    operation = args[0] if args and args[0] in ('backup', 'verify', 'recover', 'check-recovery') else 'backup'
    try:
        if not args or args[0] != operation:
            raise BackupError('CLI_USAGE', operation)
        required = {'--backup', '--backup-id'}
        if operation == 'backup':
            required |= {'--store', '--store-id'}
        if operation in ('recover', 'check-recovery'):
            required |= {'--recovery', '--new-store-id'}
        rest = args[1:]
        values = {}
        if len(rest) != 2 * len(required):
            raise BackupError('CLI_USAGE', operation)
        for key, value in zip(rest[::2], rest[1::2]):
            if key not in required or key in values:
                raise BackupError('CLI_USAGE', operation)
            values[key] = value
        if set(values) != required:
            raise BackupError('CLI_USAGE', operation)
        b = (values['--backup'], values['--backup-id'])
        if operation == 'backup':
            result = create_backup(values['--store'], values['--store-id'], *b)
        elif operation == 'verify':
            result = verify_backup(*b)
        else:
            result = {'recover': recover_backup, 'check-recovery': check_recovery}[operation](*b, values['--recovery'], values['--new-store-id'])
        code = 0
    except BackupError as error:
        result = acknowledgement(operation, error.code)
        code = 2
    except Exception:
        result = acknowledgement(operation, 'INTERNAL_ERROR')
        code = 2
    sys.stdout.buffer.write(result + b'\n')
    return code
if __name__ == '__main__':
    sys.exit(main())
