"""FieldRoutes client — read-only, ported from the IS Dashboard.

Tenant quirks (all verified live, see docs/DISCOVERY.md):
- search is per-office (officeIDs required or results silently drop); get is global
- a get with exactly one ID returns nothing — pad to two
- datetimes are office-local (OFFICE_TIMEZONE), not UTC
- some search filters are silently ignored — check `ignoredParams`
- the rate limit (reads per minute and per day) is per account: shared with the
  IS Dashboard and anything else using the same key
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


class FieldRoutesError(RuntimeError):
    pass


def chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class FieldRoutesClient:
    def __init__(self, base_url: str, auth_key: str, auth_token: str,
                 timeout: float = 60.0, max_reads_per_min: int = 300):
        self.base_url = base_url.rstrip("/")
        self._auth = {"authenticationKey": auth_key, "authenticationToken": auth_token}
        self._http = httpx.Client(timeout=timeout)
        self._min_interval = 60.0 / max_reads_per_min
        self._last_call = 0.0
        self.calls = 0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "FieldRoutesClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def request(self, entity: str, action: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/{entity}/{action}"
        last_err: Exception | None = None
        for attempt in range(5):
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            self.calls += 1
            try:
                resp = self._http.get(url, params={**self._auth, **(params or {})})
            except httpx.HTTPError as e:
                last_err = e
                time.sleep(2 ** attempt)
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = FieldRoutesError(f"{entity}/{action} -> HTTP {resp.status_code}")
                log.warning("FR %s/%s HTTP %s, retry %d/4",
                            entity, action, resp.status_code, attempt + 1)
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise FieldRoutesError(
                    f"{entity}/{action} -> HTTP {resp.status_code}: {resp.text[:300]}")
            body = resp.json()
            if isinstance(body, dict):
                # FR echoes authenticationKey/Token back in 'params' — never let
                # credentials travel further than this client
                body.pop("params", None)
            return body
        raise FieldRoutesError(f"{entity}/{action} failed after 5 attempts: {last_err}")

    @staticmethod
    def _ids(ids: list) -> str:
        items = [str(i) for i in ids]
        if len(items) == 1:
            items = items * 2
        return ",".join(items)

    # ---- searches (per office) -------------------------------------------

    def search_ids(self, entity: str, office_id: str, id_key: str,
                   **filters: Any) -> list[str]:
        params: dict[str, Any] = {"officeIDs": office_id}
        for k, v in filters.items():
            params[k] = v if isinstance(v, str) else json.dumps(v)
        got = self.request(entity, "search", params)
        ignored = got.get("ignoredParams") or {}
        if any(k in ignored for k in filters):
            log.warning("FR %s/search ignored filters: %s", entity, ignored)
        return [str(i) for i in (got.get(id_key) or [])]

    def between(self, field: str, since: str, until: str) -> dict:
        return {field: {"operator": "BETWEEN", "value": [since, until]}}

    # ---- batched gets (global) -------------------------------------------

    def get_records(self, entity: str, id_param: str, ids: list[str],
                    result_key: str, batch: int = 200) -> list[dict]:
        out: list[dict] = []
        seen_ids = list(dict.fromkeys(str(i) for i in ids if str(i) not in ("", "0")))
        for b in chunks(seen_ids, batch):
            got = self.request(entity, "get", {id_param: self._ids(b)})
            recs = got.get(result_key)
            if isinstance(recs, dict):
                recs = list(recs.values())
            out.extend(recs or [])
        return out

    def get_subscriptions(self, ids: list[str]) -> list[dict]:
        return self.get_records("subscription", "subscriptionIDs", ids, "subscriptions")

    def get_customers(self, ids: list[str]) -> list[dict]:
        return self.get_records("customer", "customerIDs", ids, "customers")

    def get_appointments(self, ids: list[str]) -> list[dict]:
        return self.get_records("appointment", "appointmentIDs", ids, "appointments")

    def get_tickets(self, ids: list[str]) -> list[dict]:
        return self.get_records("ticket", "ticketIDs", ids, "tickets")

    def get_payments(self, ids: list[str]) -> list[dict]:
        return self.get_records("payment", "paymentIDs", ids, "payments")

    def get_employees(self, ids: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for e in self.get_records("employee", "employeeIDs", ids, "employees"):
            name = f"{(e.get('fname') or '').strip()} {(e.get('lname') or '').strip()}".strip()
            out[str(e.get("employeeID"))] = name
        return out

    def get_notes(self, ids: list[str]) -> list[dict]:
        return self.get_records("note", "noteIDs", ids, "notes")
