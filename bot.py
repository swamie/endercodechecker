import discord
from discord import app_commands
import asyncio
import aiohttp
from dataclasses import dataclass, field
from typing import List, Optional
from playwright.async_api import async_playwright
import os
import time

DISCORD_TOKEN = "YOUR_BOT_TOKEN_HERE"
ACCOUNTS_FILE = "accounts.txt"
SESSIONS_DIR = "sessions"
DELAY = 7.5
TOKEN_REFRESH_INTERVAL = 45 * 60  # refresh every 45 min (tokens last ~1hr)

# ── token store ──────────────────────────────────────────────────────────────

@dataclass
class TokenBucket:
    token: str
    account: str
    acquired_at: float = field(default_factory=time.time)
    last_used: float = 0
    rate_limited_until: float = 0
    failure_count: int = 0

token_buckets: List[TokenBucket] = []
token_lock = asyncio.Lock()

# ── playwright token acquisition ─────────────────────────────────────────────

async def acquire_token_for_account(email: str, password: str, session_path: str) -> Optional[str]:
    captured = {}

    async with async_playwright() as p:
        ctx_opts = {"storage_state": session_path} if os.path.exists(session_path) else {}
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(**ctx_opts)

        async def on_request(request):
            auth = request.headers.get("authorization")
            if auth and "purchase.mp.microsoft.com" in request.url:
                captured["token"] = auth

        context.on("request", on_request)
        page = await context.new_page()

        try:
            await page.goto("https://login.live.com", timeout=30000)

            # Only fill credentials if not already logged in
            if "login.live.com" in page.url:
                email_input = page.locator('input[name="loginfmt"]:visible')
                if await email_input.count() > 0:
                    await email_input.fill(email)
                    await page.click('input[type="submit"]')
                    await page.wait_for_timeout(1500)

                passwd_input = page.locator('input[name="passwd"]:visible')
                if await passwd_input.count() > 0:
                    await passwd_input.fill(password)
                    await page.click('input[type="submit"]')
                    await page.wait_for_timeout(2000)

                # Handle "Stay signed in?" prompt
                stay_btn = page.locator('input[id="idBtn_Back"], input[value="No"]')
                if await stay_btn.count() > 0:
                    await stay_btn.first.click()
                    await page.wait_for_timeout(1000)

            # Navigate to billing page to trigger purchase.mp token
            await page.goto("https://account.microsoft.com/billing/redeem", timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)

            # Save session so next run skips login
            await context.storage_state(path=session_path)

        except Exception as e:
            print(f"[{email}] Error during login: {e}", flush=True)
        finally:
            await browser.close()

    return captured.get("token")


async def load_all_tokens():
    os.makedirs(SESSIONS_DIR, exist_ok=True)

    try:
        with open(ACCOUNTS_FILE) as f:
            accounts = [line.strip() for line in f if ":" in line.strip()]
    except FileNotFoundError:
        print(f"{ACCOUNTS_FILE} not found.", flush=True)
        return

    print(f"Acquiring tokens for {len(accounts)} account(s)...", flush=True)

    async with token_lock:
        token_buckets.clear()

    async def process(line):
        email, password = line.split(":", 1)
        email, password = email.strip(), password.strip()
        session_path = os.path.join(SESSIONS_DIR, f"{email}.json")

        print(f"  Logging in: {email}", flush=True)
        token = await acquire_token_for_account(email, password, session_path)

        if token:
            async with token_lock:
                token_buckets.append(TokenBucket(token=token, account=email))
            print(f"  ✓ Token acquired: {email}", flush=True)
        else:
            print(f"  ✗ Failed to get token: {email} (2FA? Wrong credentials? Page didn't trigger API call)", flush=True)

    # Run account logins concurrently (2 at a time to avoid detection)
    sem = asyncio.Semaphore(2)
    async def guarded(line):
        async with sem:
            await process(line)

    await asyncio.gather(*[guarded(a) for a in accounts])
    print(f"Token load complete. {len(token_buckets)} token(s) active.", flush=True)


async def token_refresh_loop():
    while True:
        await asyncio.sleep(TOKEN_REFRESH_INTERVAL)
        print("Refreshing tokens...", flush=True)
        await load_all_tokens()


# ── key checker ───────────────────────────────────────────────────────────────

def get_available_bucket() -> Optional[TokenBucket]:
    now = time.monotonic()
    available = [b for b in token_buckets if now >= b.rate_limited_until]
    return min(available, key=lambda b: b.last_used) if available else None


async def check_key(session: aiohttp.ClientSession, key: str, sem: asyncio.Semaphore) -> str:
    async with sem:
        for attempt in range(3):
            async with token_lock:
                bucket = get_available_bucket()

            if bucket is None:
                await asyncio.sleep(5)
                continue

            now = time.monotonic()
            wait = DELAY - (now - bucket.last_used)
            if wait > 0:
                await asyncio.sleep(wait)

            bucket.last_used = time.monotonic()
            url = f"https://purchase.mp.microsoft.com/v7.0/tokenDescriptions/{key}?market=US&language=en-US"

            try:
                async with session.get(url, headers={"Authorization": bucket.token}, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status == 429:
                        bucket.rate_limited_until = time.monotonic() + 60
                        if attempt < 2:
                            await asyncio.sleep(10)
                            continue
                        return f"{key} | Rate limited"
                    elif r.status == 401:
                        bucket.failure_count += 1
                        return f"{key} | Auth expired"
                    elif r.status == 403:
                        return f"{key} | Wrong country"
                    elif r.status == 404:
                        return f"{key} | Invalid"
                    elif r.status == 200:
                        data = await r.json()
                        state = data.get("tokenState", "Unknown")
                        if state == "Active":
                            state = "Not Redeemed"
                        bucket.failure_count = 0
                        return f"{key} | {state}"
                    else:
                        return f"{key} | HTTP {r.status}"
            except asyncio.TimeoutError:
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
                return f"{key} | Timeout"
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
                return f"{key} | Error: {e}"
        return f"{key} | Failed"


async def run_checks(keys: List[str]) -> List[str]:
    sem = asyncio.Semaphore(max(len(token_buckets) * 2, 5))
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=100)) as session:
        return await asyncio.gather(*[check_key(session, k, sem) for k in keys])


# ── discord ───────────────────────────────────────────────────────────────────

class CheckCodesModal(discord.ui.Modal, title="Check Microsoft Keys"):
    codes = discord.ui.TextInput(
        label="Paste your codes (one per line)",
        style=discord.TextStyle.paragraph,
        placeholder="XXXXX-XXXXX-XXXXX-XXXXX-XXXXX",
        required=True,
        max_length=4000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)

        if not token_buckets:
            await interaction.followup.send(
                "No tokens loaded yet. Make sure `accounts.txt` exists and the bot has finished startup.",
                ephemeral=True
            )
            return

        keys = [line.strip() for line in self.codes.value.splitlines() if line.strip()]
        if not keys:
            await interaction.followup.send("No codes found.", ephemeral=True)
            return

        est = len(keys) * DELAY / max(len(token_buckets), 1) / 60
        await interaction.followup.send(
            f"Checking **{len(keys)}** code(s) with **{len(token_buckets)}** token(s) — ~{est:.1f} min",
            ephemeral=True
        )

        results = await run_checks(keys)

        bad_keywords = {"Invalid", "Auth expired", "Rate limited", "Timeout", "Error", "HTTP", "Wrong", "Failed"}
        invalid = [r for r in results if any(k in r for k in bad_keywords)]
        valid_count = len(results) - len(invalid)

        if not invalid:
            msg = f"All **{len(keys)}** codes are valid / not redeemed."
        else:
            lines = "\n".join(invalid)
            msg = f"**{valid_count}/{len(keys)}** OK.\n\n**Issues:**\n```\n{lines}\n```"

        if len(msg) > 2000:
            for chunk in [msg[i:i+1900] for i in range(0, len(msg), 1900)]:
                await interaction.followup.send(chunk, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)


intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@tree.command(name="checkcodes", description="Check Microsoft product keys")
async def checkcodes(interaction: discord.Interaction):
    await interaction.response.send_modal(CheckCodesModal())


@tree.command(name="tokenstatus", description="Show how many tokens are loaded")
async def tokenstatus(interaction: discord.Interaction):
    if not token_buckets:
        await interaction.response.send_message("No tokens loaded.", ephemeral=True)
    else:
        lines = "\n".join(f"• {b.account}" for b in token_buckets)
        await interaction.response.send_message(
            f"**{len(token_buckets)} token(s) active:**\n{lines}", ephemeral=True
        )


@tree.command(name="refreshtokens", description="Re-login all accounts and refresh tokens now")
async def refreshtokens(interaction: discord.Interaction):
    await interaction.response.send_message("Refreshing tokens...", ephemeral=True)
    await load_all_tokens()
    await interaction.followup.send(f"Done. {len(token_buckets)} token(s) active.", ephemeral=True)


@client.event
async def on_ready():
    await tree.sync()
    print(f"Bot ready: {client.user}", flush=True)
    asyncio.create_task(load_all_tokens())
    asyncio.create_task(token_refresh_loop())


client.run(DISCORD_TOKEN)
