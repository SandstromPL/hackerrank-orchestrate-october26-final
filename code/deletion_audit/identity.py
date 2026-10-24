"""Resolve a request email to the subject and every identifier they have used."""

from __future__ import annotations

from collections import defaultdict

from .dataset import AuditLogRow, IdentityRow
from .domain import Subject
from .evidence.identifiers import normalize


class IdentityIndex:
    """Current snapshot + audit history, indexed by every current or historical identifier."""

    def __init__(self, identities: list[IdentityRow], audit_log: list[AuditLogRow]) -> None:
        self._rows = {row.user_id: row for row in identities}
        self._history: dict[str, list[AuditLogRow]] = defaultdict(list)
        for entry in audit_log:
            self._history[entry.user_id].append(entry)
        self._owners: dict[str, set[str]] = defaultdict(set)
        for user_id in sorted(self._rows):
            for key in self.subject(user_id).keys:
                self._owners[key].add(user_id)

    def subject(self, user_id: str) -> Subject:
        row = self._rows[user_id]
        emails = {row.email}
        hashes = {row.email_hash}
        phones = {row.phone}
        for entry in self._history.get(user_id, []):
            target = {"email": emails, "email_hash": hashes, "phone": phones}.get(entry.field_name)
            if target is not None:
                target.update({entry.old_value, entry.new_value})
        return Subject(
            user_id=user_id,
            name=row.name,
            emails=frozenset(filter(None, (normalize(e) for e in emails))),
            email_hashes=frozenset(filter(None, (normalize(h) for h in hashes))),
            phones=frozenset(filter(None, (normalize(p) for p in phones))),
        )

    def resolve(self, email: str) -> Subject | None:
        """The unique subject whose current or historical email matches, else None."""
        key = normalize(email)
        owners = self._owners.get(key or "", set())
        if len(owners) != 1:
            return None
        return self.subject(next(iter(owners)))

    def owners(self, key: str) -> set[str]:
        """Users who have ever used this (canonical) identifier."""
        return self._owners.get(key, set())
