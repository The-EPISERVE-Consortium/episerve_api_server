"""Users, their rights and their personal tokens.

A user is a row of the ``users`` table in the ``benchmarks`` database (hand-provisioned, see README).
Rights are one word per feature, stored comma-separated; ``admin`` includes all of them.

A token is ``<user id>.<YYYYMMDD>.<signature>``. It is valid on that UTC day only, and the signature covers
the password hash, so changing the password or disabling the user invalidates every token at once.

Manage users on the server with ``python -m app.users`` (add, list, passwd, rights, disable, enable).
"""

import argparse
import getpass
import hashlib
import hmac
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app import db
from app.config import settings

RIGHTS = ("runs", "benchmarks", "blackboard", "admin")

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


@dataclass(frozen=True)
class User:
    id: int
    email: str
    name: str
    rights: frozenset
    password_hash: str = ""

    def can(self, right: str | None) -> bool:
        return right is None or "admin" in self.rights or right in self.rights


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
        return scheme == "scrypt" and hmac.compare_digest(actual, bytes.fromhex(digest))
    except ValueError:
        return False


# --- store ---

def _row_to_user(row: dict | None) -> User | None:
    if not row or not row["active"]:
        return None
    rights = frozenset(r for r in (row["rights"] or "").split(",") if r)
    return User(row["id"], row["email"], row["name"], rights, row["password_hash"])


def _query(sql: str, params: tuple) -> dict | None:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()
    finally:
        conn.close()


def _execute(sql: str, params: tuple) -> int:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            count = cur.rowcount
        conn.commit()
        return count
    finally:
        conn.close()


_COLUMNS = "id, email, name, password_hash, rights, active"


def get_by_id(user_id: int) -> User | None:
    """The active user with this id, or None."""
    return _row_to_user(_query(f"SELECT {_COLUMNS} FROM users WHERE id = %s", (user_id,)))


def get_by_email(email: str) -> User | None:
    """The active user with this email (case-insensitive), or None."""
    return _row_to_user(_query(f"SELECT {_COLUMNS} FROM users WHERE email = %s", (email.strip().lower(),)))


def list_users() -> list[dict]:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, email, name, rights, active FROM users ORDER BY id")
            return list(cur.fetchall())
    finally:
        conn.close()


def _check_rights(rights: list[str]) -> str:
    unknown = [r for r in rights if r not in RIGHTS]
    if unknown:
        raise ValueError(f"unknown right(s): {', '.join(unknown)}; known: {', '.join(RIGHTS)}")
    return ",".join(dict.fromkeys(rights))


def add_user(email: str, name: str, password: str, rights: list[str]) -> None:
    _execute("INSERT INTO users (email, name, password_hash, rights) VALUES (%s, %s, %s, %s)",
             (email.strip().lower(), name, hash_password(password), _check_rights(rights)))


def set_password(email: str, password: str) -> bool:
    return _execute("UPDATE users SET password_hash = %s WHERE email = %s",
                    (hash_password(password), email.strip().lower())) > 0


def set_rights(email: str, rights: list[str]) -> bool:
    return _execute("UPDATE users SET rights = %s WHERE email = %s",
                    (_check_rights(rights), email.strip().lower())) > 0


def set_active(email: str, active: bool) -> bool:
    return _execute("UPDATE users SET active = %s WHERE email = %s", (int(active), email.strip().lower())) > 0


# --- login and tokens ---

# Verified against when the email is unknown, so the response time does not reveal which emails exist.
_DUMMY_HASH = hash_password("not a password")


def authenticate(email: str, password: str) -> User | None:
    user = get_by_email(email)
    ok = verify_password(password, user.password_hash if user else _DUMMY_HASH)
    return user if user and ok else None


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _sign(user_id: int, day: str, password_hash: str) -> str:
    message = f"{user_id}|{day}|{password_hash}".encode()
    return hmac.new(settings.auth_master_secret.encode(), message, hashlib.sha256).hexdigest()


def issue_token(user: User) -> str:
    day = _today()
    return f"{user.id}.{day}.{_sign(user.id, day, user.password_hash)}"


def token_expires_at() -> datetime:
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(days=1)


def verify_token(token: str | None) -> User | None:
    """The user a valid token of today belongs to, or None."""
    try:
        user_id, day, signature = (token or "").split(".")
        if day != _today():
            return None
        user = get_by_id(int(user_id))
    except ValueError:
        return None
    if user is None or not hmac.compare_digest(signature, _sign(user.id, day, user.password_hash)):
        return None
    return user


# --- command line ---

def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.users", description="Manage EPISERVE users.")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="create a user (asks for the password)")
    add.add_argument("email")
    add.add_argument("name")
    add.add_argument("--rights", default="", help=f"comma-separated: {', '.join(RIGHTS)}")
    sub.add_parser("list", help="show all users")
    for command, helptext in (("passwd", "set a new password"), ("disable", "block a user"),
                              ("enable", "unblock a user")):
        sub.add_parser(command, help=helptext).add_argument("email")
    rights = sub.add_parser("rights", help="replace the rights of a user")
    rights.add_argument("email")
    rights.add_argument("rights", help="comma-separated, may be empty")
    args = parser.parse_args(argv)

    def split(text: str) -> list[str]:
        return [r.strip() for r in text.split(",") if r.strip()]

    try:
        if args.command == "list":
            for u in list_users():
                print(f"{u['id']:>3}  {u['email']:<36} {u['name']:<24} {u['rights'] or '-':<30} "
                      f"{'active' if u['active'] else 'DISABLED'}")
            return 0
        done = False
        if args.command == "add":
            password = getpass.getpass("Password: ")
            add_user(args.email, args.name, password, split(args.rights))
            done = True
        elif args.command == "passwd":
            done = set_password(args.email, getpass.getpass("New password: "))
        elif args.command == "rights":
            done = set_rights(args.email, split(args.rights))
        else:
            done = set_active(args.email, args.command == "enable")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print("done" if done else "no such user, or nothing changed", file=sys.stderr)
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
