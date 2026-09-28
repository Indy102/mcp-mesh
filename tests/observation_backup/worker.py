"""Synthetic subprocess fault worker; no production checkpoint controls."""
import os
import signal
import sys
from observation_backup import backup,create_backup,recover_backup

operation,checkpoint,*args=sys.argv[1:]
def stop(name):
    if name==checkpoint: os.kill(os.getpid(),signal.SIGKILL)
backup._checkpoint=stop
if operation=='backup': create_backup(*args)
else: recover_backup(*args)
