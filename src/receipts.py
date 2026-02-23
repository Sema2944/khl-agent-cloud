# src/receipts.py
"""
Automatic receipt generation via API "Мой налог" (lknpd.nalog.ru).

Self-employed (самозанятый) must issue a receipt for every payment (422-ФЗ).
This module handles auth, token caching, and receipt creation.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

API_URL = "https://lknpd.nalog.ru/api/v1/"
DEVICE_ID = "betly-bot-server"
INN = "330405238055"
MSK = timezone(timedelta(hours=3))

_token_cache: dict = {
    "token": None,
    "refresh_token": None,
    "expires_at": 0.0,
}


def _auth(inn: str, password: str) -> dict:
    """Authenticate via INN + password from nalog.ru personal cabinet."""
    resp = requests.post(
        API_URL + "auth/lkfl",
        json={
            "inn": inn,
            "password": password,
            "deviceInfo": {
                "appVersion": "1.0.0",
                "sourceDeviceId": DEVICE_ID,
                "sourceType": "android",
                "metaDetails": {"browser": "", "browserVersion": "", "os": "android"},
            },
        },
        timeout=15,
    )
    resp.raise_for_status()
    d = resp.json()
    return {"token": d["token"], "refresh_token": d["refreshToken"]}


def _do_refresh(rt: str) -> dict:
    """Refresh expired token."""
    resp = requests.post(
        API_URL + "auth/token",
        json={
            "deviceInfo": {
                "appVersion": "1.0.0",
                "sourceDeviceId": DEVICE_ID,
                "sourceType": "android",
                "metaDetails": {"browser": "", "browserVersion": "", "os": "android"},
            },
            "refreshToken": rt,
        },
        timeout=15,
    )
    resp.raise_for_status()
    d = resp.json()
    return {"token": d["token"], "refresh_token": d["refreshToken"]}


def _get_token() -> str:
    """Get a valid token, refreshing or re-authing as needed."""
    now = time.time()

    # Cached token still valid (with 60s margin)
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    # Try refresh
    if _token_cache["refresh_token"]:
        try:
            data = _do_refresh(_token_cache["refresh_token"])
            _token_cache["token"] = data["token"]
            _token_cache["refresh_token"] = data["refresh_token"]
            _token_cache["expires_at"] = now + 3500  # ~1 hour
            return data["token"]
        except Exception:
            logger.debug("Token refresh failed, will re-auth")

    # Full auth
    password = (os.getenv("NALOG_PASSWORD") or "").strip()
    if not password:
        raise RuntimeError("NALOG_PASSWORD env var is not set")

    data = _auth(INN, password)
    _token_cache["token"] = data["token"]
    _token_cache["refresh_token"] = data["refresh_token"]
    _token_cache["expires_at"] = now + 3500
    return data["token"]


def create_receipt(
    amount: float,
    service_name: str,
    client_name: str | None = None,
) -> str:
    """Create a receipt in 'Мой налог' and return the receipt URL.

    Args:
        amount: Payment amount in rubles (e.g. 299.0)
        service_name: Description of the service
        client_name: Optional buyer display name

    Returns:
        URL to the printable receipt
    """
    token = _get_token()
    now = datetime.now(MSK)

    payload = {
        "paymentType": "ACCOUNT",  # cashless (via YooKassa)
        "ignoreMaxTotalIncomeRestriction": False,
        "client": {
            "contactPhone": None,
            "displayName": client_name or "Покупатель",
            "incomeType": "FROM_INDIVIDUAL",  # from individual (4% NPD)
            "inn": None,
        },
        "requestTime": now.isoformat(),
        "operationTime": now.isoformat(),
        "services": [
            {
                "name": service_name,
                "amount": str(amount),
                "quantity": 1,
            }
        ],
        "totalAmount": str(amount),
    }

    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(
        API_URL + "income",
        json=payload,
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()

    receipt_uuid = resp.json()["approvedReceiptUuid"]
    receipt_url = f"https://lknpd.nalog.ru/api/v1/receipt/{INN}/{receipt_uuid}/print"

    logger.info("Receipt created: uuid=%s amount=%.0f", receipt_uuid, amount)
    return receipt_url
