"""
Thin wrapper around the TradeMind REST API (tm-server). Deliberately dumb --
no retries beyond the batch-then-individual fallback for trade creation, no
caching. All JSON key names below were read directly from the current
tm-server DTOs (NewTradeRequest, TradeResponse, NewAssetRequest,
UpdateAssetRequest, AssetResponse, NewTradeStepRequest) -- keep them in sync
if those DTOs change.
"""
from __future__ import annotations

import sys
from typing import Any, Optional

import requests


class TmApiError(RuntimeError):
    pass


class TmApiClient:
    def __init__(self, base_url: str, token: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers["Content-Type"] = "application/json"
        if token:
            self._session.headers["Authorization"] = f"Bearer {token}"

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v1{path}"

    # -- Assets --------------------------------------------------------

    def get_assets(self) -> list[dict[str, Any]]:
        resp = self._session.get(self._url("/asset"))
        resp.raise_for_status()
        return resp.json()

    def create_asset(self, name: str, symbol: str, contract_size: Optional[float]) -> dict[str, Any]:
        payload = {"name": name, "symbol": symbol, "contractSize": contract_size}
        resp = self._session.post(self._url("/asset"), json=payload)
        resp.raise_for_status()
        return resp.json()

    def backfill_contract_size(self, asset_id: int, contract_size: float) -> dict[str, Any]:
        """UpdateAssetRequest treats null fields as 'leave unchanged', so we
        only ever send contractSize here -- name/symbol stay untouched."""
        payload = {"name": None, "symbol": None, "contractSize": contract_size}
        resp = self._session.put(self._url(f"/asset/{asset_id}"), json=payload)
        resp.raise_for_status()
        return resp.json()

    # -- Trades ----------------------------------------------------------

    def create_trades(self, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """POST /trade takes a List<NewTradeRequest> and returns a matching
        List<TradeResponse> in the same order. We try the whole batch first;
        if the server rejects the batch (e.g. one row fails bean validation
        and the whole request 400s), we fall back to posting each trade
        individually so a single bad row doesn't sink every other trade in
        the run, and we can report exactly which one(s) failed."""
        if not payloads:
            return []

        try:
            resp = self._session.post(self._url("/trade"), json=payloads)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as batch_error:
            print(
                f"WARNING: batch trade creation failed ({batch_error}); "
                f"retrying {len(payloads)} trade(s) individually so we can "
                f"isolate the failure(s).",
                file=sys.stderr,
            )

        results: list[dict[str, Any]] = []
        for i, payload in enumerate(payloads):
            try:
                resp = self._session.post(self._url("/trade"), json=[payload])
                resp.raise_for_status()
                created = resp.json()
                results.extend(created)
            except requests.HTTPError as row_error:
                external_id = payload.get("externalId", "?")
                body = getattr(row_error.response, "text", "")
                print(
                    f"ERROR: failed to create trade #{i} (externalId={external_id}): "
                    f"{row_error} -- {body}",
                    file=sys.stderr,
                )
        return results

    # -- Capital (deposits/withdrawals) ---------------------------------

    def create_capitals(self, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """POST /capital takes a List<NewCapitalRequest> and returns a
        matching List<CapitalResponse>. Same batch-then-individual-fallback
        shape as create_trades, for the same reason -- one bad row (e.g. a
        validation error) shouldn't sink every other balance operation in
        the run."""
        if not payloads:
            return []

        try:
            resp = self._session.post(self._url("/capital"), json=payloads)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as batch_error:
            print(
                f"WARNING: batch capital creation failed ({batch_error}); "
                f"retrying {len(payloads)} capital row(s) individually so we "
                f"can isolate the failure(s).",
                file=sys.stderr,
            )

        results: list[dict[str, Any]] = []
        for i, payload in enumerate(payloads):
            try:
                resp = self._session.post(self._url("/capital"), json=[payload])
                resp.raise_for_status()
                created = resp.json()
                results.extend(created)
            except requests.HTTPError as row_error:
                external_id = payload.get("externalId", "?")
                body = getattr(row_error.response, "text", "")
                print(
                    f"ERROR: failed to create capital row #{i} (externalId={external_id}): "
                    f"{row_error} -- {body}",
                    file=sys.stderr,
                )
        return results

    # -- Trade steps -------------------------------------------------------

    def create_trade_steps(self, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not payloads:
            return []
        try:
            resp = self._session.post(self._url("/tradeStep"), json=payloads)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            body = getattr(e.response, "text", "")
            print(f"WARNING: failed to create trade steps: {e} -- {body}", file=sys.stderr)
            return []
