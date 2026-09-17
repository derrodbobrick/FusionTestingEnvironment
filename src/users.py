"""
Who is testing, and who is allowed to set the testing up.

Two separate ideas, deliberately kept apart:

    the roster    a list of named testers, held on the share. Picking your
                  name is a selection, not a login -- there is no password and
                  none is wanted. Its job is attribution: every pass and fail
                  carries a name and a timestamp so results can be read months
                  later and traced to a person.

    the admin PIN a single shared passcode that unlocks scenario setup and
                  roster editing. Its job is to stop a tester wandering into
                  the configuration by accident, not to withstand an attacker.
                  Anyone with the PIN and access to the share can already edit
                  the files directly, so treating this as security would be
                  self-deception. It is a gate on a door, not a lock on a safe.

Both live on the shared testing folder so a roster set up once on the VM is
what every tester's copy shows.

A note on permissions. On the UAT share BUILTIN\\Users can create files but not
modify or delete them, so an ordinary tester account CANNOT write the roster or
the admin file even holding the PIN. Setup is therefore an operator-machine
job, and the service says so plainly when a write is refused rather than
failing with a bare OSError.
"""

import hashlib
import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path

PBKDF2_ROUNDS = 240000
MAX_USERS = 200


def _root() -> Path:
    import deployment
    d = deployment.testing_base()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _users_file() -> Path:
    return _root() / 'users.json'


def _admin_file() -> Path:
    return _root() / 'admin.json'


def _now() -> str:
    return datetime.now().isoformat(sep=' ', timespec='seconds')


def _write_atomic(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding='utf-8')
    os.replace(str(tmp), str(path))


class ReadOnlyShare(Exception):
    """The account running this copy may read the share but not change it."""


def _save_users(users):
    try:
        _write_atomic(_users_file(), users)
    except (PermissionError, OSError) as e:
        raise ReadOnlyShare(
            'This machine cannot write the shared roster (%s). Testers can '
            'create files on the UAT share but not change existing ones -- '
            'edit the roster from the pipeline VM.' % e.__class__.__name__)


# ------------------------------------------------------------------- the roster

def _user_id(name: str) -> str:
    base = re.sub(r'[^a-z0-9]+', '-', (name or '').strip().lower()).strip('-')
    return base[:40] or 'user'


def list_users(include_inactive: bool = False) -> list:
    p = _users_file()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return []
    users = data.get('users', data) if isinstance(data, dict) else data
    if not isinstance(users, list):
        return []
    out = [u for u in users if isinstance(u, dict) and u.get('id')]
    if not include_inactive:
        out = [u for u in out if u.get('active', True)]
    out.sort(key=lambda u: (u.get('name') or '').lower())
    return out


def get_user(user_id: str):
    uid = (user_id or '').strip()
    return next((u for u in list_users(include_inactive=True) if u['id'] == uid), None)


def add_user(name: str, note: str = '') -> dict:
    name = (name or '').strip()
    if not name:
        raise ValueError('A name is required.')
    if len(name) > 60:
        raise ValueError('That name is too long; keep it under 60 characters.')
    users = list_users(include_inactive=True)
    if len(users) >= MAX_USERS:
        raise ValueError('The roster is full (%d).' % MAX_USERS)
    if any((u.get('name') or '').lower() == name.lower() for u in users):
        raise ValueError('%s is already on the roster.' % name)

    # Ids are stable and human-readable, so a result file written today still
    # reads sensibly if someone is renamed later.
    base = _user_id(name)
    uid, n = base, 1
    taken = {u['id'] for u in users}
    while uid in taken:
        n += 1
        uid = '%s-%d' % (base, n)

    entry = {'id': uid, 'name': name, 'note': (note or '').strip(),
             'active': True, 'created_at': _now()}
    users.append(entry)
    _save_users(users)
    return entry


def update_user(user_id: str, name=None, note=None, active=None) -> dict:
    users = list_users(include_inactive=True)
    target = next((u for u in users if u['id'] == user_id), None)
    if target is None:
        raise ValueError('No such user %r.' % user_id)
    if name is not None and str(name).strip():
        new = str(name).strip()
        if any(u is not target and (u.get('name') or '').lower() == new.lower()
               for u in users):
            raise ValueError('%s is already on the roster.' % new)
        target['name'] = new
    if note is not None:
        target['note'] = str(note).strip()
    if active is not None:
        target['active'] = bool(active)
    target['updated_at'] = _now()
    _save_users(users)
    return target


def remove_user(user_id: str) -> dict:
    """Retire a tester rather than erasing them.

    Their past decisions name them, and deleting the roster entry would leave
    those results attributed to someone the app claims never existed.
    """
    return update_user(user_id, active=False)


# --------------------------------------------------------------- the admin PIN

def admin_configured() -> bool:
    return _admin_file().is_file()


def _load_admin() -> dict:
    try:
        return json.loads(_admin_file().read_text(encoding='utf-8'))
    except Exception:
        return {}


def _hash(pin: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac('sha256', pin.encode('utf-8'), salt,
                               PBKDF2_ROUNDS).hex()


def set_admin_pin(new_pin: str, current_pin: str = '') -> bool:
    """Set or change the PIN. Changing it requires the current one."""
    new_pin = (new_pin or '').strip()
    if len(new_pin) < 4:
        raise ValueError('Use at least 4 characters.')
    if admin_configured() and not check_admin_pin(current_pin):
        raise ValueError('The current PIN is not right.')
    salt = secrets.token_bytes(16)
    payload = {'algo': 'pbkdf2_sha256', 'iterations': PBKDF2_ROUNDS,
               'salt': salt.hex(), 'hash': _hash(new_pin, salt),
               'updated_at': _now()}
    try:
        _write_atomic(_admin_file(), payload)
    except (PermissionError, OSError) as e:
        raise ReadOnlyShare(
            'This machine cannot write the shared admin file (%s). Change the '
            'PIN from the pipeline VM.' % e.__class__.__name__)
    return True


def check_admin_pin(pin: str) -> bool:
    data = _load_admin()
    salt, expected = data.get('salt'), data.get('hash')
    if not salt or not expected:
        return False
    try:
        candidate = hashlib.pbkdf2_hmac(
            'sha256', (pin or '').encode('utf-8'), bytes.fromhex(salt),
            int(data.get('iterations', PBKDF2_ROUNDS))).hex()
    except Exception:
        return False
    return secrets.compare_digest(candidate, expected)
