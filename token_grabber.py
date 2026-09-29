"""
Gets a purchase.mp.microsoft.com token via Xbox Live auth chain.
Requires a registered Azure AD app with Xbox Live permissions.
Run once to set up, then reuse the token for key checking.
"""
import sys
import subprocess
import json
from pathlib import Path

TOKEN_FILE = Path(r"C:\Users\meka3\Desktop\test code\token.txt")
CONFIG_FILE = Path(r"C:\Users\meka3\Desktop\test code\grabber_config.json")

def ensure(pkg):
    try:
        __import__(pkg)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg],
                              stdout=subprocess.DEVNULL)

def get_client_id():
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text())
        cid = cfg.get("client_id", "")
        if cid:
            print(f"Using saved client_id: {cid}", flush=True)
            return cid

    print("=" * 60)
    print("FIRST-TIME SETUP: Register an Azure AD app (free, 3 min)")
    print("=" * 60)
    print()
    print("1. Go to: https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade")
    print("2. Click 'New registration'")
    print("3. Name: KeyChecker (or anything)")
    print("4. Account types: 'Personal Microsoft accounts only'")
    print("5. Redirect URI:")
    print("     Platform: 'Mobile and desktop applications'")
    print("     URI: https://login.microsoftonline.com/common/oauth2/nativeclient")
    print("6. Click 'Register'")
    print("7. Copy the 'Application (client) ID' from the overview page")
    print()
    print("Then add Xbox permissions:")
    print("8.  Left menu -> 'API permissions'")
    print("9.  Click 'Add a permission'")
    print("10. Tab: 'APIs my organization uses'")
    print("11. Search: Xbox Live")
    print("12. Select it, check Xbl.signin and Xbl.offline_access")
    print("13. Click 'Add permissions'")
    print()

    cid = input("Paste your Application (client) ID here: ").strip()
    if not cid:
        print("No client ID entered.", flush=True)
        sys.exit(1)

    CONFIG_FILE.write_text(json.dumps({"client_id": cid}))
    print(f"Saved to {CONFIG_FILE.name} — won't ask again.\n", flush=True)
    return cid


def main():
    ensure("msal")
    ensure("requests")
    import msal
    import requests

    client_id = get_client_id()

    # ── Step 1: MSA sign-in with Xbox scopes ─────────────────────────────
    print("\nStep 1/4  MSA sign-in (browser will open)...", flush=True)
    app = msal.PublicClientApplication(
        client_id,
        authority="https://login.microsoftonline.com/consumers",
    )
    result = app.acquire_token_interactive(
        scopes=["Xbl.signin", "Xbl.offline_access"],
        prompt="select_account",
    )

    if "access_token" not in result:
        err = result.get("error_description", result.get("error", str(result)))
        print(f"  FAILED: {err[:300]}", flush=True)
        if "unauthorized_client" in str(err).lower():
            print("\n  The client_id was rejected. Double-check:", flush=True)
            print("  - Account type is 'Personal Microsoft accounts only'", flush=True)
            print("  - Redirect URI is set correctly", flush=True)
            CONFIG_FILE.unlink(missing_ok=True)
            print("  Config cleared — run again after fixing.\n", flush=True)
        return

    msa_token = result["access_token"]
    print("  ok\n", flush=True)

    # ── Step 2: Xbox Live user token ─────────────────────────────────────
    print("Step 2/4  Xbox Live user token...", flush=True)
    r = requests.post("https://user.auth.xboxlive.com/user/authenticate", json={
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
        "Properties": {
            "AuthMethod": "RPS",
            "SiteName": "user.auth.xboxlive.com",
            "RpsTicket": f"d={msa_token}",
        }
    }, headers={"Content-Type": "application/json", "x-xbl-contract-version": "1"})

    if r.status_code != 200:
        print(f"  FAILED ({r.status_code}): {r.text[:400]}", flush=True)
        return

    xbl = r.json()
    xbl_token = xbl["Token"]
    uhs = xbl["DisplayClaims"]["xui"][0]["uhs"]
    print(f"  ok  (userhash {uhs})\n", flush=True)

    # ── Step 3: XSTS token for purchase.mp ───────────────────────────────
    print("Step 3/4  XSTS token for purchase.mp.microsoft.com...", flush=True)
    r = requests.post("https://xsts.auth.xboxlive.com/xsts/authorize", json={
        "RelyingParty": "http://purchase.mp.microsoft.com",
        "TokenType": "JWT",
        "Properties": {
            "UserTokens": [xbl_token],
            "SandboxId": "RETAIL",
        }
    }, headers={"Content-Type": "application/json", "x-xbl-contract-version": "1"})

    if r.status_code != 200:
        print(f"  FAILED ({r.status_code}): {r.text[:400]}", flush=True)
        return

    xsts = r.json()
    xsts_token = xsts["Token"]
    print("  ok\n", flush=True)

    # ── Step 4: test both auth formats ───────────────────────────────────
    print("Step 4/4  Testing against purchase.mp...", flush=True)
    test_url = ("https://purchase.mp.microsoft.com/v7.0/tokenDescriptions/"
                "XXXXX-XXXXX-XXXXX-XXXXX-XXXXX?market=US&language=en-US")

    formats = [
        ("XBL3.0", f"XBL3.0 x={uhs};{xsts_token}"),
        ("Bearer", f"Bearer {xsts_token}"),
    ]

    working = None
    for label, hdr in formats:
        t = requests.get(test_url, headers={"Authorization": hdr})
        ok = t.status_code in (200, 404)
        print(f"  {label}: {t.status_code} {'WORKS' if ok else ''}", flush=True)
        if ok and not working:
            working = hdr
            if t.status_code == 404:
                print("    (404 = fake key rejected, auth accepted)", flush=True)

    if not working:
        print(f"\n  Neither format accepted:", flush=True)
        print(f"  {t.text[:400]}", flush=True)
        return

    # ── Save ─────────────────────────────────────────────────────────────
    TOKEN_FILE.write_text(working + "\n")
    print(f"\n✓ Working token saved to token.txt", flush=True)
    print("  Run the dashboard to check codes.", flush=True)


if __name__ == "__main__":
    main()
