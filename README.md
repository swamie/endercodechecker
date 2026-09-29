# EnderCodeChecker

Microsoft product key checker with a live web dashboard. Validates keys against `purchase.mp.microsoft.com` with rate-limited multi-token support.

## Setup

```bash
pip install flask aiohttp playwright
playwright install chromium
```

## Token Setup

You need a valid Bearer token for `purchase.mp.microsoft.com`. Add it to `token.txt` in the project root, one token per line:

```
Bearer EwBIBMl6BAAU...
```

More tokens = faster checking (~8 keys/min per token with 7.5s rate limit).

## Usage

### Web Dashboard

```bash
python dashboard.py
```

Open `http://localhost:5000` — paste codes, click Check, results stream in live.

### CLI Checker

```bash
python file-jm4.py
```

Reads keys from `keys.txt`, tokens from `token.txt`, writes results to a file you specify.

### Token Grabber

```bash
python token_grabber.py
```

Attempts to acquire a `purchase.mp.microsoft.com` token via Xbox Live auth chain (MSA OAuth → XBL → XSTS). Requires a registered Azure AD app with Xbox Live API permissions.

### Discord Bot

```bash
python bot.py
```

Slash command `/checkcodes` opens a modal to paste and check keys. Requires a Discord bot token and `accounts.txt` with Microsoft account credentials.

## Files

| File | Description |
|------|-------------|
| `dashboard.py` | Flask backend with SSE streaming |
| `index.html` | Dark-themed dashboard frontend |
| `token_grabber.py` | Token acquisition via Xbox Live auth |
| `file-jm4.py` | Original async CLI key checker |
| `bot.py` | Discord bot interface |
| `token.txt` | Your Bearer tokens (not tracked by git) |

## How It Works

1. Each key is sent to `purchase.mp.microsoft.com/v7.0/tokenDescriptions/{key}`
2. Response indicates key status: Active (not redeemed), Redeemed, Invalid, etc.
3. Rate limiting enforces 7.5s between requests per token to avoid 429s
4. Multiple tokens are rotated for parallel checking

## Result Codes

- **Not Redeemed** — valid, unused key
- **Invalid** — key doesn't exist
- **Auth Expired** — token needs refreshing
- **Rate Limited** — too many requests, auto-retries
- **Wrong Country** — key region mismatch
