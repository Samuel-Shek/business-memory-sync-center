"""Read-only DingTalk message hydration, using reviewed CLI leaves only.

This module does not download files, reuse mail download sessions, or transfer
login state. Windows resource acquisition is a separate deployment prerequisite.
"""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json
import subprocess
from typing import Callable

from handoff_core import Rejected


def count(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    raise Rejected("response_invalid")


def identifier(value):
    if (not isinstance(value, str) or not value or len(value) > 2048
            or any(ord(c) < 32 for c in value) or "," in value or value.startswith("-")):
        raise Rejected("identity_mismatch")
    return value


class DingTalkSource:
    def __init__(self, binary: str, organization: str, profile: str, run: Callable = subprocess.run):
        self.binary = binary
        self.organization = identifier(organization)
        self.profile = identifier(profile)
        self.run = run

    def _call(self, args):
        try:
            result = self.run([self.binary, *args, "--format", "json"], capture_output=True, timeout=45)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Rejected("source_unavailable") from exc
        if result.returncode:
            # Raw stderr can contain source data. Do not put it in the ledger.
            raise Rejected("source_unavailable")
        if len(result.stdout) > 4 * 1024 * 1024:
            raise Rejected("response_too_large")
        try:
            payload = json.loads(result.stdout)
        except (UnicodeError, ValueError, TypeError) as exc:
            raise Rejected("response_invalid") from exc
        if not isinstance(payload, dict):
            raise Rejected("response_invalid")
        return payload

    def verify_profile(self):
        data = self._call(["profile", "list"])
        profiles = data.get("profiles")
        if not isinstance(profiles, list):
            raise Rejected("profile_unavailable")
        matches = [p for p in profiles if isinstance(p, dict)
                   and p.get("isOrgCurrent") is True and p.get("isCurrent") is True]
        if (data.get("success") is not True or len(matches) != 1
                or matches[0].get("profile") != self.profile
                or matches[0].get("corpId") != self.organization
                or matches[0].get("status") != "active"
                or data.get("currentProfile") != self.profile):
            raise Rejected("profile_mismatch")

    def hydrate(self, message_id: str, conversation_id: str) -> dict:
        message_id, conversation_id = identifier(message_id), identifier(conversation_id)
        self.verify_profile()
        payload = self._call(["--profile", self.profile, "chat", "+messages-mget", "--msg-ids", message_id])
        if (payload.get("complete") is not True or payload.get("hasMore") is not False
                or payload.get("failures") != [] or payload.get("notFoundMessageIds") != []
                or count(payload.get("failedCount")) != 0 or count(payload.get("notFoundCount")) != 0
                or count(payload.get("requestedCount")) != 1 or count(payload.get("foundCount")) != 1):
            raise Rejected("source_incomplete")
        rows = payload.get("messages")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise Rejected("source_incomplete")
        raw = rows[0]
        if raw.get("messageId") != message_id or raw.get("conversationId") != conversation_id:
            raise Rejected("identity_mismatch")
        refs = raw.get("resourceRefs")
        if not isinstance(refs, list):
            raise Rejected("resources_unknown")
        for ref in refs:
            if not isinstance(ref, dict) or ref.get("type") not in {"fileId", "mediaId"}:
                raise Rejected("resource_type_unknown")
            identifier(ref.get("resourceId"))
        # Never merge the event's missing [] into a complete detail response.
        # Raw text remains source evidence, not an instruction or confirmed fact.
        return raw
