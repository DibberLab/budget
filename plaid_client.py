"""
Optional Plaid integration.

Plaid lets you sync transactions automatically from most US/CA banks. To use it:

1. Sign up at https://dashboard.plaid.com/signup -- the Development tier is free
   for personal use (up to 100 linked items).
2. In the Plaid dashboard, grab your client_id and secret.
3. Set environment variables before running the app:
       export PLAID_CLIENT_ID=...
       export PLAID_SECRET=...
       export PLAID_ENV=sandbox    # or "development" / "production"
4. Visit /plaid/link in the app to connect your first institution.

If env vars are missing, the rest of the app still works perfectly -- you'll
just need to use manual entry or CSV import.

Plaid's flow has three steps that confuse first-timers:
  - link_token: short-lived token your *browser* uses to open Plaid Link.
  - public_token: what Link gives you back after the user logs in.
  - access_token: what we exchange the public_token for and store permanently.

We use the modern /transactions/sync endpoint which gives us a cursor we can
re-use to fetch only new/changed transactions on each sync.
"""
import os
from datetime import date, timedelta

PLAID_CLIENT_ID = os.environ.get("PLAID_CLIENT_ID", "")
PLAID_SECRET = os.environ.get("PLAID_SECRET", "")
PLAID_ENV = os.environ.get("PLAID_ENV", "sandbox")


def is_configured() -> bool:
    return bool(PLAID_CLIENT_ID and PLAID_SECRET)


def _client():
    """Build a Plaid API client. Imported lazily so the app works without
    the plaid-python package installed."""
    from plaid.api import plaid_api
    from plaid.configuration import Configuration
    from plaid.api_client import ApiClient

    host_map = {
        "sandbox": "https://sandbox.plaid.com",
        "development": "https://development.plaid.com",
        "production": "https://production.plaid.com",
    }
    config = Configuration(
        host=host_map.get(PLAID_ENV, host_map["sandbox"]),
        api_key={"clientId": PLAID_CLIENT_ID, "secret": PLAID_SECRET},
    )
    return plaid_api.PlaidApi(ApiClient(config))


def create_link_token(user_id: str = "local-user") -> str:
    """Returns a link_token to hand to the Plaid Link JS SDK in the browser."""
    from plaid.model.link_token_create_request import LinkTokenCreateRequest
    from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
    from plaid.model.country_code import CountryCode
    from plaid.model.products import Products

    req = LinkTokenCreateRequest(
        products=[Products("transactions")],
        client_name="Local Budget",
        country_codes=[CountryCode("US")],
        language="en",
        user=LinkTokenCreateRequestUser(client_user_id=user_id),
    )
    resp = _client().link_token_create(req)
    return resp["link_token"]


def exchange_public_token(public_token: str) -> tuple[str, str]:
    """Exchange the short-lived public_token for a permanent access_token.
    Returns (access_token, item_id)."""
    from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
    req = ItemPublicTokenExchangeRequest(public_token=public_token)
    resp = _client().item_public_token_exchange(req)
    return resp["access_token"], resp["item_id"]


def get_accounts(access_token: str) -> list[dict]:
    """List the accounts in a linked Item."""
    from plaid.model.accounts_get_request import AccountsGetRequest
    req = AccountsGetRequest(access_token=access_token)
    resp = _client().accounts_get(req)
    out = []
    for a in resp["accounts"]:
        out.append({
            "account_id": a["account_id"],
            "name": a["name"],
            "official_name": a.get("official_name"),
            "type": str(a["type"]),
            "subtype": str(a.get("subtype")),
            "balance": a["balances"].get("current"),
        })
    return out


def sync_transactions(access_token: str, cursor: str | None = None) -> dict:
    """
    Pull all new/modified/removed transactions since `cursor`.
    Returns {added, modified, removed, next_cursor}.
    """
    from plaid.model.transactions_sync_request import TransactionsSyncRequest

    added, modified, removed = [], [], []
    has_more = True
    next_cursor = cursor

    while has_more:
        req = TransactionsSyncRequest(
            access_token=access_token,
            cursor=next_cursor or "",
        )
        resp = _client().transactions_sync(req)
        added.extend(resp["added"])
        modified.extend(resp["modified"])
        removed.extend(resp["removed"])
        has_more = resp["has_more"]
        next_cursor = resp["next_cursor"]

    return {
        "added": added,
        "modified": modified,
        "removed": removed,
        "next_cursor": next_cursor,
    }
