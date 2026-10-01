# Hermes

Microsoft product key checker with a live web dashboard. Validates keys against `purchase.mp.microsoft.com` with multi-token parallel checking, live streaming results, and automatic export to `.txt`.

## Requirements

- Python 3.9+
- pip

## Install Dependencies

```bash
pip install flask aiohttp beautifulsoup4
```

Or all at once from the included file:

```bash
pip install -r requirements.txt
```

## Setup

### 1. Activation Key

Hermes requires an activation key on first launch. Enter it in the lock screen when you open the dashboard. Contact **wqp.e** (`1225822912959742099`) on Discord for the activation key.

### 2. Add Bearer Tokens

Create `token.txt` in the project root — one token per line:

```
Bearer EwBIBMl6BAAU...
Bearer EwBIBMl6BAAU...
```

More tokens = more parallel workers = faster throughput. Add tokens through the dashboard UI or directly to `token.txt`.

### 3. Run

```bash
python dashboard.py
```

Open `http://localhost:5000`

## Usage

1. Paste your Bearer token(s) into **Token Management** → click **Add Tokens**
2. Paste product keys into the **Product Keys** box (one per line)
3. Click **▶ Check Keys** — results stream in live
4. Use **⏸ Pause** / **■ Stop** to control the job mid-run
5. Results are automatically exported to `.txt` when the job finishes

## Output Files

Two files are written to the project folder on completion:

| File | Contents |
|------|----------|
| `{n}redeemcodes_{id}.txt` | Redeemed keys only |
| `results_{id}.txt` | Everything else (valid, invalid, failed, rate limited) |

Each line format:
```
KEY | Status | Redeemed: <date> | Checked: 2026-10-02 14:35:22 GMT+1
```

## Result Codes

| Status | Meaning |
|--------|---------|
| **Not Redeemed** | Valid, unused key |
| **Redeemed** | Already redeemed — includes date if available |
| **Invalid** | Key doesn't exist |
| **Auth Expired** | Bearer token needs refreshing |
| **Wrong Country** | Key region mismatch |
| **Rate Limited** | Auto-retried; add more tokens to avoid |

## Files

| File | Description |
|------|-------------|
| `dashboard.py` | Flask backend + full Hermes UI (self-contained) |
| `token.txt` | Your Bearer tokens — gitignored, never committed |
| `file-jm4.py` | Original async CLI key checker |
| `token_grabber.py` | Token acquisition via Xbox Live auth chain |
| `bot.py` | Discord bot interface (`/checkcodes`) |

## How It Works

1. Keys are sent concurrently to `purchase.mp.microsoft.com/v7.0/tokenDescriptions/{key}`
2. Bearer tokens are rotated across parallel workers with per-token rate limiting
3. Results stream to the browser in real time via Server-Sent Events
4. On completion, results are split and saved to timestamped `.txt` files
