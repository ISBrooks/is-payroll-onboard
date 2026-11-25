"""Google Sheets hub client (service account, read/write).

One spreadsheet holds everything: the payroll-adjustments Form writes its
responses tab; we READ that tab and WRITE our own output tabs ("Payroll",
"Verified Adjustments"). We never write to the Form's own tab — Forms owns
its structure.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
from google.auth.transport.requests import Request as GARequest
from google.oauth2 import service_account

from .config import Settings

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
API = "https://sheets.googleapis.com/v4/spreadsheets"


class SheetHub:
    def __init__(self, s: Settings):
        if not s.gsheet_id:
            raise RuntimeError("GSHEET_ID is not set in .env")
        self.sheet_id = s.gsheet_id
        self._creds = service_account.Credentials.from_service_account_file(
            str(s.google_sa_json), scopes=SCOPES)
        self._http = httpx.Client(timeout=60)

    def close(self) -> None:
        self._http.close()

    def _headers(self) -> dict:
        if not self._creds.valid:
            self._creds.refresh(GARequest())
        return {"Authorization": f"Bearer {self._creds.token}"}

    def _req(self, method: str, path: str, **kw) -> dict:
        r = self._http.request(method, f"{API}/{self.sheet_id}{path}",
                               headers=self._headers(), **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Sheets API {r.status_code}: {r.text[:300]}")
        return r.json()

    # ---- reads -----------------------------------------------------------
    def tabs(self) -> list[dict]:
        """[{title, sheetId, rows, cols}] for every tab."""
        meta = self._req("GET", "?fields=sheets(properties)")
        return [{"title": p["title"], "sheetId": p["sheetId"],
                 "rows": p.get("gridProperties", {}).get("rowCount"),
                 "cols": p.get("gridProperties", {}).get("columnCount")}
                for p in (s["properties"] for s in meta.get("sheets", []))]

    def read(self, tab: str, rng: str = "") -> list[list[Any]]:
        """All values from a tab (or tab!range). Rows are ragged like the UI."""
        from urllib.parse import quote
        ref = quote(f"'{tab}'!{rng}" if rng else f"'{tab}'", safe="")
        out = self._req("GET", f"/values/{ref}"
                               "?valueRenderOption=UNFORMATTED_VALUE"
                               "&dateTimeRenderOption=FORMATTED_STRING")
        return out.get("values", [])

    # ---- writes (never to the Form's own tab) ----------------------------
    def ensure_tab(self, title: str) -> None:
        if any(t["title"] == title for t in self.tabs()):
            return
        self._req("POST", ":batchUpdate", json={
            "requests": [{"addSheet": {"properties": {"title": title}}}]})

    def write(self, tab: str, values: list[list[Any]], clear: bool = True) -> None:
        """Replace a tab's contents with `values` (our output tabs only).
        RAW input: text is never parsed as formulas (a note starting with
        '+' must stay text, not become #ERROR!)."""
        from urllib.parse import quote
        self.ensure_tab(tab)
        enc = quote(f"'{tab}'", safe="")
        if clear:
            self._req("POST", f"/values/{enc}:clear", json={})
        self._req("PUT", f"/values/{quote(chr(39) + tab + chr(39) + '!A1', safe='')}"
                         "?valueInputOption=RAW",
                  json={"values": values})

    def append(self, tab: str, values: list[list[Any]]) -> None:
        from urllib.parse import quote
        self.ensure_tab(tab)
        self._req("POST", f"/values/{quote(chr(39) + tab + chr(39) + '!A1', safe='')}"
                          ":append?valueInputOption=RAW"
                          "&insertDataOption=INSERT_ROWS",
                  json={"values": values})

    def write_range(self, tab: str, anchor_a1: str, values: list[list[Any]]) -> None:
        """Write a block at a specific anchor (e.g. 'F4') without clearing
        the rest of the tab."""
        from urllib.parse import quote
        self.ensure_tab(tab)
        ref = quote(f"'{tab}'!{anchor_a1}", safe="")
        self._req("PUT", f"/values/{ref}?valueInputOption=RAW",
                  json={"values": values})

    def delete_tab(self, title: str) -> None:
        for t in self.tabs():
            if t["title"] == title:
                self._req("POST", ":batchUpdate", json={
                    "requests": [{"deleteSheet": {"sheetId": t["sheetId"]}}]})
                return
