"""
RigaNest x Beds24 — one-time auth setup.

Exchanges a Beds24 "invite code" (generated in the Beds24 control panel)
for a long-life refresh token. Run this ONCE, then save the printed
refresh token into your .env as BEDS24_REFRESH_TOKEN.

How to get an invite code:
    Beds24 control panel -> SETTINGS -> Apps and Integrations -> Booking API (V2)
    -> "Creator invite code" -> pick scopes (at minimum you need:
       read:properties, write:properties, read:inventory, write:inventory,
       read:bookings, write:bookings)
    -> Generate. Copy the code (it expires in 24 hours, one-time use).

Usage:
    python get_refresh_token.py PASTE_YOUR_INVITE_CODE_HERE
"""

import sys

import requests

API_BASE = "https://beds24.com/api/v2"


def main():
    if len(sys.argv) != 2:
        print("Usage: python get_refresh_token.py <invite_code>")
        sys.exit(1)

    invite_code = sys.argv[1].strip()

    resp = requests.get(
        f"{API_BASE}/authentication/setup",
        headers={"accept": "application/json", "code": invite_code},
        timeout=20,
    )
    data = resp.json()

    if resp.status_code != 200 or "refreshToken" not in data:
        print(f"ERROR: could not exchange invite code.\nResponse: {data}")
        sys.exit(1)

    print("=" * 60)
    print("SUCCESS. Save this in your .env file:")
    print()
    print(f"BEDS24_REFRESH_TOKEN={data['refreshToken']}")
    print()
    print("(This refresh token lasts forever as long as it keeps being")
    print(" used at least once every 30 days — beds24_sync.py running")
    print(" on a schedule will keep it alive.)")
    print("=" * 60)


if __name__ == "__main__":
    main()
