"""Shared, fail-closed pause control for detector, mail and browser preparation."""
import datetime as dt
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONTROL = ROOT / '.private/availability-notifier/user-pause.json'


def is_paused(path=None):
    path = Path(path or CONTROL)
    try:
        return json.loads(path.read_text()).get('paused') is not False
    except FileNotFoundError:
        return False
    except (OSError, ValueError, TypeError, AttributeError):
        return True


def set_paused(paused, *, reason, path=None):
    path = Path(path or CONTROL)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = {'paused':bool(paused), 'at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
             'resume_requires_user_instruction':True, 'reason':reason}
    temporary = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    temporary.chmod(0o600)
    temporary.replace(path)
    return value
