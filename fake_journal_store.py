"""
In-memory stand-in for journal_data.SqliteJournalStore, for unit tests.

Plain lists of dicts, no database connection of any kind. Each read applies the
same simple filter as its SQL counterpart and each write has the same effect as
its statement - nothing more; the decisions live in journal.py. Test support
only: keep it in step with SqliteJournalStore by hand (the SQL itself is only
checkable against a real database).
"""

import copy


class InMemoryJournalStore:
    def __init__(self, legacy_extra_income=None):
        self._loans = []
        self._terms = []
        self._observations = []
        self._entries = []
        self._legacy = list(legacy_extra_income or [])
        self.legacy_table_dropped = False
        self.commits = 0

    def close(self):
        pass

    def commit(self):
        self.commits += 1

    # -- loans --------------------------------------------------------------- #

    def add_loan(self, name, created_at):
        loan = {"id": len(self._loans) + 1, "name": name, "created_at": created_at, "closed_at": None}
        self._loans.append(loan)
        return loan["id"]

    def loans(self):
        return [
            {k: l[k] for k in ("id", "name", "created_at")} for l in self._loans if l["closed_at"] is None
        ]

    def close_loan(self, loan_id, closed_at):
        for loan in self._loans:
            if loan["id"] == loan_id and loan["closed_at"] is None:
                loan["closed_at"] = closed_at

    def add_terms(self, loan_id, effective_from, annual_rate_pct, emi_lakhs, note, created_at):
        row = {
            "id": len(self._terms) + 1, "loan_id": loan_id, "effective_from": effective_from,
            "annual_rate_pct": annual_rate_pct, "emi_lakhs": emi_lakhs, "note": note,
            "created_at": created_at, "voided_at": None,
        }
        self._terms.append(row)
        return row["id"]

    def terms(self, loan_id):
        rows = [t for t in self._terms if t["loan_id"] == loan_id and t["voided_at"] is None]
        rows.sort(key=lambda t: (t["effective_from"], t["id"]))
        return [{k: v for k, v in t.items() if k != "voided_at"} for t in rows]

    def void_terms(self, terms_id, voided_at):
        for t in self._terms:
            if t["id"] == terms_id and t["voided_at"] is None:
                t["voided_at"] = voided_at

    # -- observations -------------------------------------------------------- #

    def add_observation(self, bucket, ref_id, as_of, amount_lakhs, source, note, created_at):
        row = {
            "id": len(self._observations) + 1, "bucket": bucket, "ref_id": ref_id, "as_of": as_of,
            "amount_lakhs": amount_lakhs, "source": source, "note": note, "created_at": created_at,
            "voided_at": None,
        }
        self._observations.append(row)
        return row["id"]

    def observations(self, bucket=None, ref_id=None):
        rows = [
            o for o in self._observations
            if o["voided_at"] is None
            and (bucket is None or o["bucket"] == bucket)
            and (ref_id is None or o["ref_id"] == ref_id)
        ]
        rows.sort(key=lambda o: (o["as_of"], o["id"]))
        return [{k: v for k, v in o.items() if k != "voided_at"} for o in rows]

    def void_observation(self, observation_id, voided_at):
        for o in self._observations:
            if o["id"] == observation_id and o["voided_at"] is None:
                o["voided_at"] = voided_at

    # -- ledger entries ------------------------------------------------------ #

    def add_entry(self, entry):
        row = {
            "id": len(self._entries) + 1, "ref_id": None, "from_bucket": None, "to_bucket": None,
            "status": "done", "approximate": False, "note": None, "source": "journal",
            "legacy_year": None, "voided_at": None, **copy.deepcopy(entry),
        }
        row["approximate"] = bool(row["approximate"])
        self._entries.append(row)
        return row["id"]

    def entries(self, kind=None, include_voided=False):
        rows = [
            e for e in self._entries
            if (include_voided or e["voided_at"] is None) and (kind is None or e["kind"] == kind)
        ]
        rows.sort(key=lambda e: (e["entry_date"], e["id"]))
        return copy.deepcopy(rows)

    def entry(self, entry_id):
        for e in self._entries:
            if e["id"] == entry_id:
                return copy.deepcopy(e)
        return None

    def void_entry(self, entry_id, voided_at):
        for e in self._entries:
            if e["id"] == entry_id and e["voided_at"] is None:
                e["voided_at"] = voided_at

    def set_entry_status(self, entry_id, status):
        for e in self._entries:
            if e["id"] == entry_id:
                e["status"] = status

    # -- legacy -------------------------------------------------------------- #

    def legacy_extra_income_rows(self):
        return copy.deepcopy(self._legacy)

    def drop_legacy_extra_income_table(self):
        self._legacy = []
        self.legacy_table_dropped = True
