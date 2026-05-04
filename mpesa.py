"""
M-Pesa Daraja API integration (Safaricom).

Set these environment variables before going live:
  MPESA_CONSUMER_KEY      - from Safaricom Developer Portal
  MPESA_CONSUMER_SECRET   - from Safaricom Developer Portal
  MPESA_PASSKEY           - Lipa na M-Pesa passkey
  MPESA_SHORTCODE         - your till/paybill number
  MPESA_ENV               - 'sandbox' (default) or 'production'
  MPESA_CALLBACK_URL      - publicly reachable URL for payment confirmations

Sandbox credentials can be generated at:
  https://developer.safaricom.co.ke/
"""
import os
import base64
import requests
from datetime import datetime
from loguru import logger


class MpesaError(Exception):
    pass


SANDBOX_BASE = 'https://sandbox.safaricom.co.ke'
PRODUCTION_BASE = 'https://api.safaricom.co.ke'


def _base_url() -> str:
    env = os.getenv('MPESA_ENV', 'sandbox').lower()
    return PRODUCTION_BASE if env == 'production' else SANDBOX_BASE


def _get_access_token() -> str:
    consumer_key = os.getenv('MPESA_CONSUMER_KEY', '')
    consumer_secret = os.getenv('MPESA_CONSUMER_SECRET', '')

    if not consumer_key or not consumer_secret:
        raise MpesaError(
            "MPESA_CONSUMER_KEY and MPESA_CONSUMER_SECRET must be set as environment variables."
        )

    credentials = base64.b64encode(f"{consumer_key}:{consumer_secret}".encode()).decode()
    url = f"{_base_url()}/oauth/v1/generate?grant_type=client_credentials"

    try:
        response = requests.get(url, headers={"Authorization": f"Basic {credentials}"}, timeout=10)
        response.raise_for_status()
        token = response.json().get('access_token')
        if not token:
            raise MpesaError("No access_token in M-Pesa response")
        return token
    except requests.RequestException as exc:
        raise MpesaError(f"Failed to obtain M-Pesa access token: {exc}") from exc


def _generate_password() -> tuple[str, str]:
    shortcode = os.getenv('MPESA_SHORTCODE', '174379')
    passkey = os.getenv('MPESA_PASSKEY', '')
    if not passkey:
        raise MpesaError("MPESA_PASSKEY must be set as an environment variable.")
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    raw = f"{shortcode}{passkey}{timestamp}"
    password = base64.b64encode(raw.encode()).decode()
    return password, timestamp


def stk_push(phone: str, amount: float, account_reference: str, description: str) -> dict:
    """
    Initiate an STK Push (Lipa na M-Pesa Online).

    Args:
        phone: Customer phone in international format without '+', e.g. '254712345678'
        amount: Amount in KES (rounded to nearest integer)
        account_reference: Short reference shown to customer, e.g. 'SolidarityPool'
        description: Transaction description shown in the prompt

    Returns:
        dict with CheckoutRequestID, MerchantRequestID, ResponseDescription
    """
    shortcode = os.getenv('MPESA_SHORTCODE', '174379')
    callback_url = os.getenv(
        'MPESA_CALLBACK_URL',
        'https://yourapp.replit.app/mpesa/callback'
    )

    try:
        token = _get_access_token()
        password, timestamp = _generate_password()

        payload = {
            "BusinessShortCode": shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": int(round(amount)),
            "PartyA": phone,
            "PartyB": shortcode,
            "PhoneNumber": phone,
            "CallBackURL": callback_url,
            "AccountReference": account_reference,
            "TransactionDesc": description,
        }

        url = f"{_base_url()}/mpesa/stkpush/v1/processrequest"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        response = requests.post(url, json=payload, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()

        logger.info(
            "STK Push initiated for phone={} amount={} checkout_request_id={}",
            phone, amount, data.get('CheckoutRequestID')
        )
        return data

    except MpesaError:
        raise
    except requests.RequestException as exc:
        raise MpesaError(f"STK Push request failed: {exc}") from exc


def parse_stk_callback(callback_body: dict) -> dict:
    """
    Parse the STK callback payload Safaricom sends to your callback URL.

    Returns a normalised dict:
      {
        'checkout_request_id': str,
        'result_code': int,        # 0 = success
        'result_desc': str,
        'mpesa_receipt': str | None,
        'amount': float | None,
        'phone': str | None,
      }
    """
    try:
        stk_result = callback_body['Body']['stkCallback']
        result_code = stk_result.get('ResultCode', -1)
        result_desc = stk_result.get('ResultDesc', '')
        checkout_request_id = stk_result.get('CheckoutRequestID', '')

        mpesa_receipt = None
        amount = None
        phone = None

        if result_code == 0:
            items = stk_result.get('CallbackMetadata', {}).get('Item', [])
            item_map = {i['Name']: i.get('Value') for i in items}
            mpesa_receipt = item_map.get('MpesaReceiptNumber')
            amount = item_map.get('Amount')
            phone = str(item_map.get('PhoneNumber', ''))

        return {
            'checkout_request_id': checkout_request_id,
            'result_code': result_code,
            'result_desc': result_desc,
            'mpesa_receipt': mpesa_receipt,
            'amount': amount,
            'phone': phone,
        }

    except (KeyError, TypeError) as exc:
        raise MpesaError(f"Could not parse STK callback: {exc}") from exc
