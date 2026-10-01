from flask import Flask, request, jsonify, Response, session
import asyncio, aiohttp, json, threading, queue, time, os, uuid, re, random, hashlib
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from bs4 import BeautifulSoup

# ─── Config ──────────────────────────────────────────────────────────────────

TOKEN_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.txt")
OUTPUT_DIR  = os.path.dirname(os.path.abspath(__file__))   # results saved next to dashboard.py
RATE_DELAY      = 0.3   # baseline seconds between uses of the same token
RATE_DELAY_MAX  = 2.0   # ceiling an adaptive token delay can reach (lowered — recovery was too slow at 4s)
RATE_DELAY_MIN  = 0.2   # floor — never go faster than this
GMT1        = timezone(timedelta(hours=1))

app = Flask(__name__)
app.secret_key = os.urandom(24)   # session signing — regenerated each restart (local use)

# ─── Auth ─────────────────────────────────────────────────────────────────────
# Key stored as its SHA-256 digest — plaintext never appears in source.
# Byte sequence 101-110-100-101-114 is the activation key in ASCII ordinals.
_AUTH_HASH = hashlib.sha256(bytes([101,110,100,101,114])).hexdigest()

@app.before_request
def _gate():
    free = {"/", "/api/unlock"}
    if request.path in free or session.get("unlocked"):
        return
    return jsonify(error="unauthorized"), 401

@app.route("/api/unlock", methods=["POST"])
def api_unlock():
    key = (request.json or {}).get("key", "").strip()
    if hashlib.sha256(key.encode()).hexdigest() == _AUTH_HASH:
        session["unlocked"] = True
        return jsonify(ok=True)
    return jsonify(ok=False, error="Invalid activation key"), 403

# In-memory state
token_pool: list = []
active_jobs: dict = {}
finished_jobs: dict = {}   # job_id → records, kept after SSE stream closes
seen_redeemed: list = []

# HTTP status → (display label, kind, extra)
STATUS_MAP = {
    401: ("Auth Expired", "failed", ""),
    403: ("Wrong Country", "failed", ""),
    404: ("Invalid", "invalid", ""),
}


# ─── Token Management ─────────────────────────────────────────────────────────

def load_tokens():
    """
    Reload token pool from disk, preserving usage/rate-limit state.
    New tokens get staggered initial next_use so they never all fire
    simultaneously — the #1 cause of synchronized 429 waves.
    """
    existing = {tok.token: tok for tok in token_pool}
    lines = []
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            lines = f.readlines()

    fresh = []
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        if raw in existing:
            fresh.append(existing[raw])
        else:
            fresh.append(SimpleNamespace(
                token=raw,
                next_use=0,
                rate_reset=0,
                delay=RATE_DELAY,   # per-token adaptive delay
                streak=0,           # consecutive successful requests
                frozen=False,       # True while in a 429 cooldown
            ))

    # Stagger initial scheduling: spread tokens evenly across one RATE_DELAY window
    n = len(fresh)
    now = time.monotonic()
    for i, tok in enumerate(fresh):
        if tok.next_use == 0:          # only touch brand-new tokens
            tok.next_use = now + i * (RATE_DELAY / max(n, 1))

    token_pool[:] = fresh


load_tokens()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def find_redeem_date(obj):
    """Recursively search a JSON object for a redemption date field."""
    items = (
        obj.items() if isinstance(obj, dict)
        else enumerate(obj) if isinstance(obj, list)
        else []
    )
    for key, value in items:
        key_str = str(key).lower()
        if "redeem" in key_str and ("date" in key_str or "time" in key_str):
            if isinstance(value, (str, int, float)):
                return value
        result = find_redeem_date(value)
        if result:
            return result
    return ""


# ─── Minecraft Cape Checks ────────────────────────────────────────────────────

async def check_minecraft_redemption(session, key):
    """
    Web-scrape fallback: check cape code via minecraft.net/redeem.
    Returns (status_label, kind, date_string).
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        async with session.get(
            f"https://minecraft.net/redeem/{key}",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=3),   # was 10 — cut hard
        ) as resp:
            if resp.status == 200:
                page_html = await resp.text()
                low = page_html.lower()
                if "already been redeemed" in low:
                    return "Redeemed", "redeemed", ""
                if "invalid code" in low:
                    return "Invalid", "invalid", ""
                return "Valid", "valid", ""
            if resp.status == 404:
                return "Invalid", "invalid", ""
            return f"HTTP {resp.status}", "failed", ""
    except Exception as exc:
        return str(exc) or "Timeout", "failed", ""


async def check_minecraft_cape_direct(session, key):
    """
    Primary check: Minecraft entitlements API.
    Falls back to web-scrape on any non-404 failure.
    Returns (status_label, kind, date_string).
    """
    try:
        async with session.get(
            f"https://api.minecraftservices.com/entitlements/codes/{key}",
            headers={"Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=2),   # was 5 — tight budget
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("redeemed"):
                    return "Redeemed", "redeemed", data.get("redeemedAt", "")
                return "Valid", "valid", ""
            if resp.status == 404:
                return "Invalid", "invalid", ""
            # Unexpected status — fall through to scrape
    except Exception:
        pass
    return await check_minecraft_redemption(session, key)


async def check_minecraft_cape(session, key):
    """Entry point for a single cape code: tries direct API, scrape fallback."""
    return await check_minecraft_cape_direct(session, key)


# ─── Office Redemption Check ──────────────────────────────────────────────────

async def check_office_redemption(session, key):
    """
    Attempt to verify a key's redemption status via Office setup.
    Returns (status_label, kind, date_string).
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/91.0.4472.124 Safari/537.36"
        )
    }

    try:
        # Fetch the enter-key page to grab any CSRF token
        async with session.get(
            "https://setup.office.com/redeem/enter-key",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                return "", "failed", "Failed to load Office redemption page"
            page_html = await resp.text()

        form_token = ""
        soup = BeautifulSoup(page_html, "html.parser")
        token_input = soup.find("input", {"name": re.compile(r"token", re.I)})
        if token_input:
            form_token = token_input.get("value", "")

        # Submit the key for validation
        payload = {
            "productKey": key,
            "__RequestVerificationToken": form_token,
        }
        async with session.post(
            "https://setup.office.com/redeem/validate-key",
            headers=headers,
            data=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                return f"HTTP {resp.status}", "failed", ""

            data = await resp.json()
            if not data.get("IsValid"):
                return "Invalid", "invalid", data.get("ErrorMessage", "")

            if data.get("IsRedeemed"):
                redeem_date = data.get("RedeemedDate", "")
                return "Redeemed", "redeemed", redeem_date or "Date not available"

            return "Not Redeemed", "valid", ""

    except Exception as exc:
        return str(exc) or "Timeout", "failed", ""


# ─── Key Checker ──────────────────────────────────────────────────────────────

async def check_key(session, key):
    """
    Check a single product key against the Microsoft API.
    Returns (status_label, kind, date_string).
    """
    rate_limit_hits = 0
    errors = 0

    while rate_limit_hits < 5 and errors < 3:
        now = time.monotonic()
        available = [tok for tok in token_pool if now >= tok.rate_reset]

        if not available:
            # Wait for the soonest token, with a small jitter so workers
            # don't all wake up and fire at the exact same millisecond
            wait = min(tok.rate_reset for tok in token_pool) - now
            await asyncio.sleep(max(0.1, wait) + random.uniform(0, 0.05))
            continue

        # Pick soonest-available token; use its own adaptive delay + tiny jitter
        tok = min(available, key=lambda t: t.next_use + t.delay)
        scheduled = max(now, tok.next_use + tok.delay + random.uniform(0, 0.02))
        tok.next_use = scheduled

        if scheduled > now:
            await asyncio.sleep(scheduled - now)

        url = (
            f"https://purchase.mp.microsoft.com/v7.0/tokenDescriptions/{key}"
            "?market=US&language=en-US"
        )
        try:
            async with session.get(
                url,
                headers={"Authorization": tok.token},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 429:
                    rate_limit_hits += 1
                    retry_after = resp.headers.get("Retry-After", "")
                    server_hint  = int(retry_after) if retry_after.isdigit() else 0
                    tok.delay    = min(tok.delay * 2, RATE_DELAY_MAX)
                    tok.streak   = 0
                    tok.frozen   = True
                    tok.rate_reset = time.monotonic() + max(server_hint, tok.delay)
                    continue

                if resp.status == 200:
                    data  = await resp.json()
                    state = str(data.get("tokenState", "Unknown"))

                    # First clean request after a freeze → snap straight back to baseline
                    # (don't crawl back — that took 465+ requests before)
                    if tok.frozen:
                        tok.delay  = RATE_DELAY
                        tok.frozen = False
                    else:
                        # Extended clean run → nudge slightly faster (toward floor)
                        tok.streak += 1
                        if tok.streak >= 30 and tok.delay > RATE_DELAY_MIN:
                            tok.delay  = max(RATE_DELAY_MIN, tok.delay * 0.95)
                            tok.streak = 0

                    if state.lower() == "redeemed":
                        if not seen_redeemed:
                            seen_redeemed.append(1)
                            print("First redeemed response:", json.dumps(data)[:800], flush=True)

                        redeem_date = find_redeem_date(data)
                        if not redeem_date:
                            _, office_kind, office_date = await check_office_redemption(session, key)
                            if office_kind == "redeemed":
                                redeem_date = office_date

                        return "Redeemed", "redeemed", redeem_date

                    label = "Not Redeemed" if state == "Active" else state
                    return label, "valid", ""

                return STATUS_MAP.get(resp.status, (f"HTTP {resp.status}", "failed", ""))

        except Exception as exc:
            errors += 1
            if errors > 2:
                return str(exc) or "Timeout", "failed", ""
            await asyncio.sleep(2)

    return "Rate Limited", "limited", ""


# ─── Result File Export ───────────────────────────────────────────────────────

def write_result_files(records: list, name: str = "") -> dict:
    """
    Split completed records into two text files named after the run.
    Format: D.M.YY-HH.MM-{name}-{count}.txt
    Returns dict with filenames, full paths, and export directory.
    """
    now   = datetime.now(GMT1)
    stamp = now.strftime("%-d.%-m.%y-%H.%M") if os.name != "nt" else now.strftime("%#d.%#m.%y-%H.%M")
    slug  = re.sub(r"[^\w\-]", "", name.strip().lower().replace(" ", "-")) or "export"

    redeemed = [r for r in records if r["kind"] == "redeemed"]
    others   = [r for r in records if r["kind"] != "redeemed"]

    def fmt(r):
        parts = [r["key"], r["status"]]
        if r["date"]:
            parts.append(f"Redeemed: {r['date']}")
        parts.append(f"Checked: {r['checked_at']} GMT+1")
        return " | ".join(parts)

    result = {"redeemed_file": None, "other_file": None, "export_dir": OUTPUT_DIR}

    if redeemed:
        fname = os.path.join(OUTPUT_DIR, f"{stamp}-{slug}-{len(redeemed)}.txt")
        with open(fname, "w", encoding="utf-8") as f:
            f.write("\n".join(fmt(r) for r in redeemed) + "\n")
        result["redeemed_file"] = os.path.basename(fname)

    if others:
        fname = os.path.join(OUTPUT_DIR, f"{stamp}-{slug}-others-{len(others)}.txt")
        with open(fname, "w", encoding="utf-8") as f:
            f.write("\n".join(fmt(r) for r in others) + "\n")
        result["other_file"] = os.path.basename(fname)

    return result


# ─── Batch Runner ─────────────────────────────────────────────────────────────

async def run_batch(keys, result_queue, state):
    """
    Check all keys concurrently against the Microsoft purchase API using bearer
    tokens.  Results stream as they complete — no in-order blocking.
    Supports pause and stop.
    """
    if not token_pool:
        result_queue.put({"type": "error", "message": "No tokens loaded. Add a bearer token under Token Management."})
        result_queue.put({"type": "done"})
        return

    # 3 workers per token: one in-flight, one queued, one about to grab the slot.
    # Keeps every token fully saturated with no gap between requests.
    concurrency = max(len(token_pool) * 3, 6)
    semaphore   = asyncio.Semaphore(concurrency)

    async def process_one(session, key):
        async with semaphore:
            # Fast-exit on stop
            if state.stopped.is_set():
                status, kind, date = "Stopped", "failed", ""
            else:
                # Pause loop — tight sleep so resume is instant
                while state.paused.is_set() and not state.stopped.is_set():
                    state.active += 1
                    await asyncio.sleep(0.05)
                    state.active -= 1

                if state.stopped.is_set():
                    status, kind, date = "Stopped", "failed", ""
                else:
                    state.active += 1
                    try:
                        status, kind, date = await check_key(session, key)
                    except Exception as exc:
                        status, kind, date = str(exc) or "Error", "failed", ""
                    state.active -= 1

        # Push immediately — no ordering wait (no head-of-line blocking)
        checked_at = datetime.now(GMT1).strftime("%Y-%m-%d %H:%M:%S")
        record = {
            "key":        key,
            "status":     status,
            "kind":       kind,
            "date":       date,
            "checked_at": checked_at,
        }
        state.records.append(record)
        result_queue.put({"type": "result", **record})

    connector = aiohttp.TCPConnector(
        limit=500,
        limit_per_host=0,           # no per-host cap
        ttl_dns_cache=300,          # cache DNS for 5 min
        enable_cleanup_closed=True,
    )
    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(*[process_one(session, k) for k in keys])

    # Auto-export on completion (name set later if user manually exports with a custom name)
    if state.records:
        files = write_result_files(state.records, getattr(state, "export_name", ""))
        result_queue.put({"type": "exported", **files})

    result_queue.put({"type": "done"})


# ─── API Routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return Response(DASHBOARD_HTML)


@app.route("/api/tokens")
def get_token_count():
    return jsonify(count=len(token_pool))


@app.route("/api/token", methods=["POST"])
def add_tokens():
    body = request.json or {}
    new_lines = [line.strip() for line in body.get("token", "").splitlines() if line.strip()]

    existing_raw = open(TOKEN_FILE).read() if os.path.exists(TOKEN_FILE) else ""
    existing_set = {line.strip() for line in existing_raw.splitlines()}
    to_add = [t for t in dict.fromkeys(new_lines) if t not in existing_set]

    with open(TOKEN_FILE, "a") as f:
        prefix = "\n" if existing_raw and not existing_raw.endswith("\n") else ""
        f.write(prefix + "".join(t + "\n" for t in to_add))

    load_tokens()
    return jsonify(count=len(token_pool), added=len(to_add))


@app.route("/api/check", methods=["POST"])
def start_check():
    body = request.json or {}
    keys = [k.strip() for k in body.get("codes", "").splitlines() if k.strip()]
    if not keys:
        return jsonify(error=1)

    load_tokens()
    job_id = str(uuid.uuid4())
    q = queue.Queue()
    state = SimpleNamespace(paused=threading.Event(), stopped=threading.Event(), active=0, records=[])
    active_jobs[job_id] = (q, state)

    threading.Thread(
        target=lambda: asyncio.run(run_batch(keys, q, state)),
        daemon=True,
    ).start()

    return jsonify(job_id=job_id, total=len(keys))


@app.route("/api/pause/<job_id>/<int:should_pause>", methods=["POST"])
def toggle_pause(job_id, should_pause):
    event = active_jobs[job_id][1].paused
    if should_pause:
        event.set()
    else:
        event.clear()
    return "ok"


@app.route("/api/export/<job_id>", methods=["POST"])
def export_results(job_id):
    if job_id in active_jobs:
        records = active_jobs[job_id][1].records
    elif job_id in finished_jobs:
        records = finished_jobs[job_id]
    else:
        return jsonify(error="job not found"), 404
    if not records:
        return jsonify(error="no results yet"), 400
    name = (request.json or {}).get("name", "")
    files = write_result_files(records, name)
    return jsonify(**files)


@app.route("/api/open-folder", methods=["POST"])
def open_folder():
    import subprocess
    try:
        if os.name == "nt":
            os.startfile(OUTPUT_DIR)
        else:
            subprocess.Popen(["open" if __import__("sys").platform == "darwin" else "xdg-open", OUTPUT_DIR])
    except Exception:
        pass
    return "ok"


@app.route("/api/stop/<job_id>", methods=["POST"])
def stop_job(job_id):
    if job_id in active_jobs:
        st = active_jobs[job_id][1]
        st.stopped.set()
        st.paused.clear()  # unblock any paused workers so they can exit
    return "ok"


@app.route("/api/stream/<job_id>")
def stream_results(job_id):
    # Guard: job may have finished before the browser opened the SSE connection
    if job_id not in active_jobs:
        def gone():
            yield 'data: {"type":"error","message":"Job not found or already finished"}\n\n'
            yield 'data: {"type":"done"}\n\n'
        return Response(gone(), mimetype="text/event-stream")

    q, state = active_jobs[job_id]

    def generate():
        sent_paused = False
        while True:
            try:
                item = q.get(timeout=0.5)
            except queue.Empty:
                if state.paused.is_set() and state.active < 1 and not sent_paused:
                    sent_paused = True
                    yield 'data: {"type":"paused"}\n\n'
                if not state.paused.is_set():
                    sent_paused = False
                continue

            yield f"data: {json.dumps(item)}\n\n"
            if item["type"] == "done":
                break

        # Stash records so manual export still works after stream closes
        if job_id in active_jobs:
            finished_jobs[job_id] = active_jobs.pop(job_id)[1].records

    return Response(generate(), mimetype="text/event-stream")


# ─── Frontend ─────────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hermes</title>
<style>
  /* ── Lock screen ── */
  #lock-screen {
    position: fixed; inset: 0; z-index: 9999;
    background: #07090f;
    display: flex; align-items: center; justify-content: center;
    flex-direction: column; gap: 20px;
    transition: opacity .4s;
  }
  #lock-screen.hidden { opacity: 0; pointer-events: none; }
  .lock-box {
    background: #0d1120; border: 1px solid #1c2740; border-radius: 14px;
    padding: 36px 40px; display: flex; flex-direction: column;
    align-items: center; gap: 16px; width: 340px;
  }
  .lock-title { font-family: system-ui,sans-serif; font-size: 18px; font-weight: 700; color: #e2e8f0; letter-spacing: .03em; }
  .lock-sub   { font-family: system-ui,sans-serif; font-size: 12px; color: #64748b; }
  .lock-input {
    width: 100%; padding: 10px 14px; background: #111826; border: 1px solid #1c2740;
    border-radius: 8px; font-family: 'Courier New', monospace; font-size: 14px;
    color: #e2e8f0; outline: none; text-align: center; letter-spacing: .1em;
    transition: border-color .15s;
  }
  .lock-input:focus { border-color: #3b7ff5; }
  .lock-btn {
    width: 100%; padding: 10px; background: #3b7ff5; border: none; border-radius: 8px;
    color: #fff; font-size: 13px; font-weight: 700; cursor: pointer; font-family: system-ui,sans-serif;
    transition: background .15s;
  }
  .lock-btn:hover { background: #2563d4; }
  .lock-err { font-family: system-ui,sans-serif; font-size: 12px; color: #ef4444; min-height: 16px; }

  /* ── Completion modal ── */
  #done-modal {
    position: fixed; inset: 0; z-index: 8888;
    background: rgba(7,9,15,.75); backdrop-filter: blur(4px);
    display: flex; align-items: center; justify-content: center;
    opacity: 0; pointer-events: none; transition: opacity .3s;
  }
  #done-modal.visible { opacity: 1; pointer-events: all; }
  .done-box {
    background: #0d1120; border: 1px solid #1c2740; border-radius: 16px;
    padding: 32px 36px; width: 380px; display: flex; flex-direction: column; gap: 20px;
    box-shadow: 0 24px 64px rgba(0,0,0,.6);
  }
  .done-head { display: flex; align-items: center; justify-content: space-between; }
  .done-title { font-size: 15px; font-weight: 700; color: #e2e8f0; }
  .done-close { background: none; border: none; color: #64748b; font-size: 18px; cursor: pointer; line-height: 1; padding: 2px 6px; border-radius: 4px; }
  .done-close:hover { color: #e2e8f0; background: #161f30; }
  .done-time { font-size: 12px; color: #64748b; font-family: 'JetBrains Mono', monospace; }
  .done-stats { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .done-stat {
    background: #111826; border: 1px solid #1c2740; border-radius: 10px;
    padding: 12px 14px; display: flex; flex-direction: column; gap: 3px;
  }
  .done-stat-val { font-size: 22px; font-weight: 700; font-family: 'JetBrains Mono', monospace; }
  .done-stat-lbl { font-size: 10px; font-weight: 600; letter-spacing: .07em; text-transform: uppercase; color: #64748b; }
  .done-stat.total .done-stat-val  { color: #e2e8f0; }
  .done-stat.valid .done-stat-val  { color: var(--valid); }
  .done-stat.redeemed .done-stat-val { color: var(--redeemed); }
  .done-stat.invalid .done-stat-val  { color: var(--invalid); }
  .done-stat.limited .done-stat-val  { color: var(--limited); }
  .done-stat.failed .done-stat-val   { color: var(--failed); }
  .done-rate { font-size: 12px; color: #64748b; text-align: center; font-family: 'JetBrains Mono', monospace; }
  .done-dismiss {
    width: 100%; padding: 10px; background: #3b7ff5; border: none; border-radius: 8px;
    color: #fff; font-size: 13px; font-weight: 700; cursor: pointer; font-family: system-ui,sans-serif;
    transition: background .15s;
  }
  .done-dismiss:hover { background: #2563d4; }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:           #07090f;
    --surface:      #0d1120;
    --surface-2:    #111826;
    --surface-3:    #161f30;
    --border:       #1c2740;
    --border-soft:  #253352;
    --text:         #e2e8f0;
    --text-dim:     #94a3b8;
    --text-muted:   #4a5568;
    --accent:       #3b82f6;
    --accent-dim:   rgba(59,130,246,0.12);
    --valid:        #22c55e;
    --valid-bg:     rgba(34,197,94,0.10);
    --redeemed:     #f87171;
    --redeemed-bg:  rgba(248,113,113,0.10);
    --invalid:      #fb923c;
    --invalid-bg:   rgba(251,146,60,0.10);
    --limited:      #facc15;
    --limited-bg:   rgba(250,204,21,0.10);
    --failed:       #e879f9;
    --failed-bg:    rgba(232,121,249,0.10);
    --r: 7px;
    --r-sm: 5px;
  }

  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    font-size: 13px;
    line-height: 1.5;
  }

  /* ── Layout ── */
  .app {
    max-width: 1380px;
    margin: 0 auto;
    padding: 20px 24px 40px;
    display: grid;
    grid-template-rows: auto 1fr;
    grid-template-columns: 340px 1fr;
    gap: 14px;
  }

  /* ── Header ── */
  .header {
    grid-column: 1 / -1;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-bottom: 14px;
    border-bottom: 1px solid var(--border);
  }

  .logo {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 16px;
    font-weight: 600;
    letter-spacing: -0.2px;
  }

  .logo-dot {
    width: 9px; height: 9px;
    border-radius: 50%;
    background: var(--accent);
    box-shadow: 0 0 10px var(--accent);
    flex-shrink: 0;
  }

  .token-pill {
    font-size: 11px;
    color: var(--text-muted);
    background: var(--surface-2);
    border: 1px solid var(--border);
    padding: 4px 12px;
    border-radius: 99px;
    letter-spacing: 0.02em;
  }

  /* ── Card ── */
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--r);
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }

  .card-head {
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    font-size: 10.5px;
    font-weight: 600;
    letter-spacing: 0.09em;
    text-transform: uppercase;
    color: var(--text-muted);
    display: flex;
    align-items: center;
    gap: 7px;
    flex-shrink: 0;
  }

  .card-body { padding: 14px; }

  /* ── Left column ── */
  .left { display: flex; flex-direction: column; gap: 12px; }

  /* ── Inputs ── */
  textarea {
    width: 100%;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    color: var(--text);
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 12px;
    padding: 9px 11px;
    resize: vertical;
    outline: none;
    transition: border-color 0.15s;
  }
  textarea:focus { border-color: var(--accent); }
  textarea::placeholder { color: var(--text-muted); }
  #codes-input  { height: 210px; }
  #token-input  { height: 72px; }

  /* ── Buttons ── */
  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 5px;
    padding: 8px 14px;
    border-radius: var(--r-sm);
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    border: none;
    transition: background 0.15s, opacity 0.15s;
    white-space: nowrap;
  }
  .btn:disabled { opacity: 0.35; cursor: not-allowed; }

  .btn-primary { background: var(--accent); color: #fff; }
  .btn-primary:not(:disabled):hover { background: #2563eb; }

  .btn-ghost {
    background: var(--surface-3);
    color: var(--text-dim);
    border: 1px solid var(--border);
  }
  .btn-ghost:not(:disabled):hover { border-color: var(--border-soft); color: var(--text); }

  .btn-pause {
    background: rgba(250,204,21,0.12);
    color: var(--limited);
    border: 1px solid rgba(250,204,21,0.25);
  }
  .btn-pause:not(:disabled):hover { background: rgba(250,204,21,0.18); }

  .btn-stop {
    background: rgba(239,68,68,0.12);
    color: var(--redeemed);
    border: 1px solid rgba(239,68,68,0.3);
  }
  .btn-stop:not(:disabled):hover { background: rgba(239,68,68,0.2); }

  .btn-sm { padding: 3px 9px; font-size: 11px; }

  .btn-row { display: flex; gap: 8px; margin-top: 8px; }
  .btn-row .grow { flex: 1; }

  /* ── Progress ── */
  .progress-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--r);
    padding: 12px 14px;
  }
  .progress-meta {
    display: flex;
    justify-content: space-between;
    font-size: 11.5px;
    margin-bottom: 7px;
  }
  .progress-meta span:first-child { color: var(--text-muted); }
  .progress-meta span:last-child  { font-weight: 600; font-variant-numeric: tabular-nums; }

  .progress-track {
    height: 4px;
    background: var(--border);
    border-radius: 99px;
    overflow: hidden;
  }
  .progress-bar {
    height: 100%;
    width: 0%;
    border-radius: 99px;
    background: var(--accent);
    transition: width 0.35s ease, background 0.2s;
  }
  .progress-bar.paused { background: var(--limited); }

  .progress-sub {
    margin-top: 6px;
    font-size: 11px;
    color: var(--text-muted);
    min-height: 15px;
  }

  /* ── Stats row ── */
  .stats {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 8px;
    flex-shrink: 0;
  }

  .stat {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    padding: 10px 8px;
    text-align: center;
  }

  .stat-n {
    font-size: 22px;
    font-weight: 700;
    line-height: 1.1;
    font-variant-numeric: tabular-nums;
  }
  .stat-l {
    font-size: 10px;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-top: 3px;
  }

  /* ── Right column ── */
  .right { display: flex; flex-direction: column; gap: 12px; }

  /* ── Live feed ── */
  .feed {
    flex: 1;
    overflow-y: auto;
    max-height: 340px;
    padding: 7px;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }

  .result-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 10px;
    border-radius: var(--r-sm);
    background: var(--surface-2);
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 11.5px;
    transition: background 0.1s;
  }
  .result-row:hover { background: var(--surface-3); }

  .result-key {
    flex: 1;
    color: var(--text-dim);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .badge {
    flex-shrink: 0;
    font-size: 10px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 99px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .result-date {
    color: var(--text-muted);
    font-size: 10.5px;
    white-space: nowrap;
  }

  /* ── Sorted sections ── */
  .sort-section {
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    overflow: hidden;
    margin-bottom: 5px;
  }
  .sort-section:last-child { margin-bottom: 0; }

  .sort-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 12px;
    cursor: pointer;
    user-select: none;
    background: var(--surface-2);
    transition: background 0.1s;
  }
  .sort-head:hover { background: var(--surface-3); }

  .sort-title {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    font-weight: 500;
  }
  .sort-count {
    font-size: 10px;
    padding: 1px 7px;
    border-radius: 99px;
    font-weight: 700;
  }

  .sort-keys {
    display: none;
    padding: 10px 12px;
    max-height: 130px;
    overflow-y: auto;
    background: var(--surface);
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 11px;
    color: var(--text-dim);
    white-space: pre;
    line-height: 1.7;
    border-top: 1px solid var(--border);
  }
  .sort-keys.open { display: block; }

  /* ── Empty state ── */
  .empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 8px;
    color: var(--text-muted);
    font-size: 12px;
    padding: 28px 0;
  }
  .empty-icon { font-size: 26px; opacity: 0.35; }

  /* ── Scrollbar ── */
  ::-webkit-scrollbar { width: 5px; height: 5px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border-soft); border-radius: 99px; }

  .add-msg { font-size: 11px; color: var(--valid); }

  /* ── Export name modal & confirmation ── */
  .exp-modal {
    position: fixed; inset: 0; z-index: 8800;
    background: rgba(7,9,15,.78); backdrop-filter: blur(4px);
    display: flex; align-items: center; justify-content: center;
    opacity: 0; pointer-events: none; transition: opacity .25s;
  }
  .exp-modal.visible { opacity: 1; pointer-events: all; }
  .exp-box {
    background: #0d1120; border: 1px solid #1c2740; border-radius: 14px;
    padding: 28px 32px; width: 360px; display: flex; flex-direction: column; gap: 14px;
    box-shadow: 0 20px 56px rgba(0,0,0,.55);
  }
  .exp-title { font-size: 14px; font-weight: 700; color: #e2e8f0; }
  .exp-sub   { font-size: 11px; color: #64748b; }
  .exp-input {
    width: 100%; padding: 9px 12px; background: #111826; border: 1px solid #1c2740;
    border-radius: 8px; font-size: 13px; color: #e2e8f0; outline: none;
    transition: border-color .15s; font-family: system-ui,sans-serif;
  }
  .exp-input:focus { border-color: #3b7ff5; }
  .exp-row { display: flex; gap: 8px; }
  .exp-confirm {
    flex: 1; padding: 9px; background: #3b7ff5; border: none; border-radius: 8px;
    color: #fff; font-size: 13px; font-weight: 700; cursor: pointer; font-family: system-ui,sans-serif;
    transition: background .15s;
  }
  .exp-confirm:hover { background: #2563d4; }
  .exp-cancel {
    padding: 9px 16px; background: #111826; border: 1px solid #1c2740; border-radius: 8px;
    color: #64748b; font-size: 13px; cursor: pointer; font-family: system-ui,sans-serif;
  }
  .exp-cancel:hover { color: #e2e8f0; }
  /* Confirmation */
  .exp-file-row { display: flex; flex-direction: column; gap: 6px; }
  .exp-file-label { font-size: 10px; font-weight: 600; letter-spacing: .07em; text-transform: uppercase; color: #64748b; }
  .exp-file-name { font-size: 12px; color: #3b7ff5; font-family: 'Consolas','Courier New',monospace; word-break: break-all; }
  .exp-open-btn {
    width: 100%; padding: 9px; background: none; border: 1px solid #3b7ff5; border-radius: 8px;
    color: #3b7ff5; font-size: 13px; font-weight: 600; cursor: pointer; font-family: system-ui,sans-serif;
    transition: background .15s, color .15s;
  }
  .exp-open-btn:hover { background: #3b7ff5; color: #fff; }

  /* ── Export banner ── */
  .export-banner {
    display: none;
    background: var(--surface-2);
    border: 1px solid var(--border-soft);
    border-radius: var(--r-sm);
    padding: 10px 14px;
    font-size: 12px;
    line-height: 1.8;
  }
  .export-banner.visible { display: block; }
  .export-banner strong { color: var(--text); }
  .export-file { color: var(--accent); font-family: 'Consolas','Courier New',monospace; }
</style>
</head>
<body>

<!-- ── Lock screen ── -->
<div id="lock-screen">
  <div class="lock-box">
    <div class="lock-title">Hermes</div>
    <div class="lock-sub">Enter activation key to continue</div>
    <input class="lock-input" id="lock-key" type="password" placeholder="••••••••" autocomplete="off" spellcheck="false">
    <button class="lock-btn" id="lock-btn">Unlock</button>
    <div class="lock-err" id="lock-err"></div>
  </div>
</div>

<!-- ── Completion modal ── -->
<div id="done-modal">
  <div class="done-box">
    <div class="done-head">
      <span class="done-title">✓ Run Complete</span>
      <button class="done-close" id="done-close">✕</button>
    </div>
    <div class="done-time" id="done-time"></div>
    <div class="done-stats">
      <div class="done-stat total"><div class="done-stat-val" id="ds-total">0</div><div class="done-stat-lbl">Total Checked</div></div>
      <div class="done-stat valid"><div class="done-stat-val" id="ds-valid">0</div><div class="done-stat-lbl">Valid</div></div>
      <div class="done-stat redeemed"><div class="done-stat-val" id="ds-redeemed">0</div><div class="done-stat-lbl">Redeemed</div></div>
      <div class="done-stat invalid"><div class="done-stat-val" id="ds-invalid">0</div><div class="done-stat-lbl">Invalid</div></div>
      <div class="done-stat limited"><div class="done-stat-val" id="ds-limited">0</div><div class="done-stat-lbl">Rate Limited</div></div>
      <div class="done-stat failed"><div class="done-stat-val" id="ds-failed">0</div><div class="done-stat-lbl">Failed</div></div>
    </div>
    <div class="done-rate" id="done-rate"></div>
    <button class="done-dismiss" id="done-dismiss">Dismiss</button>
  </div>
</div>

<!-- ── Export name prompt ── -->
<div class="exp-modal" id="exp-name-modal">
  <div class="exp-box">
    <div class="exp-title">Export Results</div>
    <div class="exp-sub">Enter a name for this run (e.g. "aurora") — used in the filename</div>
    <input class="exp-input" id="exp-name-input" placeholder="Run name…" maxlength="40" spellcheck="false">
    <div class="exp-row">
      <button class="exp-cancel" id="exp-name-cancel">Cancel</button>
      <button class="exp-confirm" id="exp-name-confirm">Export</button>
    </div>
  </div>
</div>

<!-- ── Export confirmation ── -->
<div class="exp-modal" id="exp-confirm-modal">
  <div class="exp-box">
    <div class="exp-title">✓ Exported</div>
    <div class="exp-file-row" id="exp-file-rows"></div>
    <button class="exp-open-btn" id="exp-open-folder">📂 Open folder</button>
    <button class="exp-confirm" id="exp-confirm-close">Done</button>
  </div>
</div>

<div class="app">

  <!-- ── Header ── -->
  <div class="header">
    <div class="logo">
      <div class="logo-dot"></div>
      Hermes
    </div>
    <span class="token-pill" id="token-summary">Loading…</span>
  </div>

  <!-- ── Left Panel ── -->
  <div class="left">

    <!-- Token management -->
    <div class="card">
      <div class="card-head">⚡ Token Management</div>
      <div class="card-body" style="display:flex;flex-direction:column;gap:8px;">
        <textarea id="token-input" placeholder="Paste bearer token(s), one per line"></textarea>
        <div style="display:flex;align-items:center;gap:10px;">
          <button class="btn btn-ghost grow" onclick="addTokens()">Add Tokens</button>
          <span class="add-msg" id="token-msg"></span>
        </div>
      </div>
    </div>

    <!-- Key input -->
    <div class="card">
      <div class="card-head">🔑 Product Keys</div>
      <div class="card-body" style="display:flex;flex-direction:column;gap:10px;">
        <textarea id="codes-input" placeholder="Paste keys here, one per line&#10;&#10;XXXXX-XXXXX-XXXXX-XXXXX-XXXXX" oninput="updateCounter()"></textarea>
        <div style="font-size:11px;color:var(--text-muted);text-align:right;margin-top:-4px" id="key-counter">0 keys</div>
        <div class="btn-row">
          <button class="btn btn-primary grow" id="check-btn" onclick="go()">▶ Check Keys</button>
          <button class="btn btn-pause" id="pause-btn" onclick="togglePause()" hidden>⏸ Pause</button>
          <button class="btn btn-stop"  id="stop-btn"  onclick="stopJob()"    hidden>■ Stop</button>
        </div>
      </div>
    </div>

    <!-- Progress -->
    <div class="progress-card">
      <div class="progress-meta">
        <span>Progress</span>
        <span id="progress-count">—</span>
      </div>
      <div class="progress-track">
        <div class="progress-bar" id="progress-bar"></div>
      </div>
      <div class="progress-sub" id="status-line"></div>
      <div class="progress-sub" id="eta-line" style="color:var(--text-dim)"></div>
    </div>

    <!-- Re-run -->
    <button class="btn btn-ghost" id="rerun-btn" onclick="rerunFailed()" disabled style="width:100%">
      Re-run failed &amp; rate limited (0)
    </button>

    <!-- Export -->
    <button class="btn btn-ghost" id="export-btn" onclick="exportNow()" disabled style="width:100%">
      💾 Export Results to .txt
    </button>
    <div class="export-banner" id="export-banner"></div>

  </div>

  <!-- ── Right Panel ── -->
  <div class="right">

    <!-- Stats -->
    <div class="stats">
      <div class="stat">
        <div class="stat-n" id="sn-valid"   style="color:var(--valid)">0</div>
        <div class="stat-l">Valid</div>
      </div>
      <div class="stat">
        <div class="stat-n" id="sn-redeemed" style="color:var(--redeemed)">0</div>
        <div class="stat-l">Redeemed</div>
      </div>
      <div class="stat">
        <div class="stat-n" id="sn-invalid"  style="color:var(--invalid)">0</div>
        <div class="stat-l">Invalid</div>
      </div>
      <div class="stat">
        <div class="stat-n" id="sn-limited"  style="color:var(--limited)">0</div>
        <div class="stat-l">Rate Ltd</div>
      </div>
      <div class="stat">
        <div class="stat-n" id="sn-failed"   style="color:var(--failed)">0</div>
        <div class="stat-l">Failed</div>
      </div>
    </div>

    <!-- Live feed -->
    <div class="card" style="flex:1">
      <div class="card-head">📡 Live Results</div>
      <div class="feed" id="output">
        <div class="empty">
          <div class="empty-icon">🔍</div>
          Results will appear here as keys are checked
        </div>
      </div>
    </div>

    <!-- Sorted -->
    <div class="card">
      <div class="card-head">📋 Sorted Results</div>
      <div style="padding:8px" id="sorted-output">
        <div class="empty" style="padding:16px 0">No results yet</div>
      </div>
    </div>

  </div>
</div>

<script>
let jobId, pauseState = 0, done = 0, total = 0;
let resultMap = {}, orderedKeys = [], elementMap = {}, busy = 0, debounce;
let firstResult = true, lastExport = null, startTime = 0;
// Running counters — O(1) updates, no filter scans
const counts = { valid:0, redeemed:0, invalid:0, limited:0, failed:0 };
// Per-kind key arrays for renderSorted — no re-filter needed
const kindKeys = { valid:[], redeemed:[], invalid:[], limited:[], failed:[] };
const FEED_LIMIT = 300; // max live-feed rows in DOM
let scrollPending = false;

function updateCounter() {
  const n = $('codes-input').value.split('\n').filter(l => l.trim()).length;
  $('key-counter').textContent = n ? n.toLocaleString() + ' key' + (n === 1 ? '' : 's') : '0 keys';
}

function fmtEta(secs) {
  secs = Math.ceil(secs);
  if (secs < 60)  return secs + 's';
  const m = Math.floor(secs / 60), s = secs % 60;
  if (m < 60) return m + 'm ' + String(s).padStart(2,'0') + 's';
  const h = Math.floor(m / 60);
  return h + 'h ' + String(m % 60).padStart(2,'0') + 'm';
}

const $ = id => document.getElementById(id);

const KC = {
  valid:    'var(--valid)',
  redeemed: 'var(--redeemed)',
  invalid:  'var(--invalid)',
  limited:  'var(--limited)',
  failed:   'var(--failed)'
};
const KB = {
  valid:    'var(--valid-bg)',
  redeemed: 'var(--redeemed-bg)',
  invalid:  'var(--invalid-bg)',
  limited:  'var(--limited-bg)',
  failed:   'var(--failed-bg)'
};
const KINDS = [
  ['valid','Valid'], ['redeemed','Redeemed'], ['invalid','Invalid'],
  ['limited','Rate Limited'], ['failed','Failed']
];

function post(url, body) {
  return fetch(url, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body||{}) });
}

function formatDate(v) {
  if (!v) return '';
  const n = +v, t = new Date(isNaN(n) ? v : n < 1e12 ? n*1000 : n);
  return isNaN(t) ? v : t.toLocaleString();
}

function setTokenLabel(n) {
  $('token-summary').textContent = n + ' token' + (n===1?'':'s') + ' loaded';
}

async function addTokens() {
  const r = await (await post('/api/token', { token: $('token-input').value })).json();
  $('token-input').value = '';
  $('token-msg').textContent = r.added > 0 ? '+' + r.added + ' added' : 'No new tokens';
  setTimeout(() => $('token-msg').textContent = '', 3000);
  setTokenLabel(r.count);
}

function renderProgress() {
  const pct = total ? Math.round(done/total*100) : 0;
  const bar = $('progress-bar');
  bar.style.width = pct + '%';
  bar.className = 'progress-bar' + (pauseState ? ' paused' : '');
  $('progress-count').textContent = total ? done.toLocaleString() + ' / ' + total.toLocaleString() + ' (' + pct + '%)' : '—';
  $('status-line').textContent = ['','Pausing…','Paused'][pauseState] || '';

  // ETA
  const etaEl = $('eta-line');
  if (!total || !done || pauseState) { etaEl.textContent = ''; return; }
  const elapsed = (Date.now() - startTime) / 1000;
  const rate = done / elapsed;                       // keys per second
  const remaining = total - done;
  if (remaining <= 0) { etaEl.textContent = ''; return; }
  const eta = remaining / rate;
  etaEl.textContent = '≈ ' + fmtEta(eta) + ' left  ·  ' + rate.toFixed(1) + ' keys/s';
}

function updateStats() {
  for (const [kind] of KINDS) {
    const el = $('sn-' + kind);
    if (el) el.textContent = counts[kind] || 0;
  }
  const n = (counts.failed || 0) + (counts.limited || 0);
  const btn = $('rerun-btn');
  btn.textContent = 'Re-run failed & rate limited (' + n + ')';
  btn.disabled = busy || !n;
}

function renderSorted() {
  const box = $('sorted-output');
  if (!orderedKeys.length) {
    box.innerHTML = '<div class="empty" style="padding:16px 0">No results yet</div>';
    updateStats(); return;
  }
  // Update existing sections in-place; build missing ones
  for (const [kind, label] of KINDS) {
    const keys = kindKeys[kind];
    let sec = box.querySelector('[data-kind="' + kind + '"]');
    if (!keys.length) { if (sec) sec.remove(); continue; }
    if (!sec) {
      sec = document.createElement('div');
      sec.className = 'sort-section';
      sec.dataset.kind = kind;
      const head = document.createElement('div');
      head.className = 'sort-head';
      head.innerHTML =
        '<div class="sort-title">' +
          '<span style="color:' + KC[kind] + '">' + label + '</span>' +
          '<span class="sort-count" style="background:' + KB[kind] + ';color:' + KC[kind] + '"></span>' +
        '</div>' +
        '<button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();cpKind(\'' + kind + '\',this)">Copy</button>';
      const pre = document.createElement('div');
      pre.className = 'sort-keys';
      head.addEventListener('click', () => pre.classList.toggle('open'));
      sec.append(head, pre);
      box.append(sec);
    }
    sec.querySelector('.sort-count').textContent = keys.length;
    const pre = sec.querySelector('.sort-keys');
    if (pre.classList.contains('open')) pre.textContent = keys.join('\n');
  }
  updateStats();
}

function cpKind(kind, btn) {
  navigator.clipboard.writeText(kindKeys[kind].join('\n'));
  btn.textContent = 'Copied!';
  setTimeout(() => btn.textContent = 'Copy', 1200);
}

function rerunFailed() {
  const keys = [...(kindKeys.failed || []), ...(kindKeys.limited || [])];
  if (keys.length) go(keys);
}

async function go(overrideKeys) {
  if (busy) return;
  if (!overrideKeys) {
    overrideKeys = [...new Set($('codes-input').value.split('\n').map(x => x.trim()).filter(Boolean))];
    if (!overrideKeys.length) return;
    resultMap = {}; orderedKeys = []; elementMap = {}; firstResult = true;
    for (const k of Object.keys(counts))  counts[k]  = 0;
    for (const k of Object.keys(kindKeys)) kindKeys[k] = [];
    $('output').innerHTML = '<div class="empty"><div class="empty-icon">⏳</div>Checking keys…</div>';
  }

  busy = 1; $('check-btn').disabled = 1;
  done = pauseState = 0; total = overrideKeys.length; startTime = Date.now();
  renderSorted(); renderProgress(); updateStats();

  const r = await (await post('/api/check', { codes: overrideKeys.join('\n') })).json();
  if (r.error) { busy = 0; $('check-btn').disabled = 0; return; }

  jobId = r.job_id;
  $('pause-btn').hidden = false;
  $('pause-btn').textContent = '⏸ Pause';
  $('pause-btn').disabled = false;
  $('stop-btn').hidden = false;
  $('stop-btn').disabled = false;
  $('export-btn').disabled = false;
  $('export-banner').classList.remove('visible');

  const es = new EventSource('/api/stream/' + jobId);
  es.onmessage = ev => {
    const msg = JSON.parse(ev.data);

    if (msg.type === 'result') {
      done++;
      if (firstResult) { firstResult = false; $('output').innerHTML = ''; }
      const key = msg.key;
      const prevKind = resultMap[key];

      // Update running counters + kind arrays
      if (prevKind) {
        counts[prevKind] = (counts[prevKind] || 1) - 1;
        const arr = kindKeys[prevKind];
        const i = arr.indexOf(key);
        if (i !== -1) arr.splice(i, 1);
      } else {
        orderedKeys.push(key);
      }
      counts[msg.kind] = (counts[msg.kind] || 0) + 1;
      kindKeys[msg.kind].push(key);
      resultMap[key] = msg.kind;

      // Live feed — cap at FEED_LIMIT rows
      let el = elementMap[key];
      if (!el) {
        el = elementMap[key] = document.createElement('div');
        el.className = 'result-row';
        const feed = $('output');
        feed.append(el);
        if (feed.children.length > FEED_LIMIT) feed.firstElementChild.remove();
        if (!scrollPending) {
          scrollPending = true;
          requestAnimationFrame(() => { feed.scrollTop = feed.scrollHeight; scrollPending = false; });
        }
      }
      const d = msg.kind === 'redeemed' ? formatDate(msg.date) : '';
      el.innerHTML =
        '<span class="result-key">' + key + '</span>' +
        '<span class="badge" style="background:' + KB[msg.kind] + ';color:' + KC[msg.kind] + '">' + msg.status + '</span>' +
        (d ? '<span class="result-date">' + d + '</span>' : '');
      renderProgress();
      clearTimeout(debounce);
      debounce = setTimeout(() => { renderSorted(); updateStats(); }, 2000);
    }

    if (msg.type === 'paused') {
      pauseState = 2; $('pause-btn').textContent = '▶ Resume'; $('pause-btn').disabled = false;
      renderProgress();
    }

    if (msg.type === 'error') $('status-line').textContent = msg.message;

    if (msg.type === 'exported') {
      showExport(msg);
    }

    if (msg.type === 'done' || msg.type === 'error') {
      es.close(); pauseState = busy = 0;
      $('pause-btn').hidden = true;
      $('stop-btn').hidden = true;
      $('check-btn').disabled = 0;
      $('export-btn').disabled = 0;
      $('eta-line').textContent = '';
      if (msg.type === 'done') { renderProgress(); showDoneModal(); }
      renderSorted(); updateStats();
    }
  };
}

function showExportConfirm(files) {
  lastExport = files;
  const rows = $('exp-file-rows');
  rows.innerHTML = '';
  const add = (label, name) => {
    if (!name) return;
    const d = document.createElement('div');
    d.className = 'exp-file-row';
    d.innerHTML = '<div class="exp-file-label">' + label + '</div>' +
                  '<div class="exp-file-name">' + name + '</div>';
    rows.appendChild(d);
  };
  add('Redeemed', files.redeemed_file);
  add('Others', files.other_file);
  $('exp-confirm-modal').classList.add('visible');
}

// Auto-export SSE notification — show confirmation immediately
function showExport(files) { showExportConfirm(files); }

// Export button → name prompt → export → confirmation
function exportNow() {
  if (!jobId) return;
  $('exp-name-input').value = '';
  $('exp-name-modal').classList.add('visible');
  setTimeout(() => $('exp-name-input').focus(), 50);
}

async function doExport() {
  const name = $('exp-name-input').value.trim();
  $('exp-name-modal').classList.remove('visible');
  const r = await (await post('/api/export/' + jobId, { name })).json();
  if (r.error) { alert('Export failed: ' + r.error); return; }
  showExportConfirm(r);
}

$('exp-name-confirm').addEventListener('click', doExport);
$('exp-name-cancel').addEventListener('click',  () => $('exp-name-modal').classList.remove('visible'));
$('exp-name-input').addEventListener('keydown', e => { if (e.key === 'Enter') doExport(); });
$('exp-confirm-close').addEventListener('click', () => $('exp-confirm-modal').classList.remove('visible'));
$('exp-open-folder').addEventListener('click',  () => post('/api/open-folder', {}));

async function togglePause() {
  if (pauseState === 1) return;
  const want = !pauseState;
  $('pause-btn').disabled = 1;
  await fetch('/api/pause/' + jobId + '/' + (+want), { method:'POST' });
  if (want) {
    if (pauseState !== 2) { pauseState = 1; $('pause-btn').textContent = '⏳ Pausing…'; }
  } else {
    pauseState = 0; $('pause-btn').textContent = '⏸ Pause'; $('pause-btn').disabled = false;
  }
  renderProgress();
}

async function stopJob() {
  if (!jobId) return;
  $('stop-btn').disabled = 1;
  $('stop-btn').textContent = '■ Stopping…';
  await fetch('/api/stop/' + jobId, { method: 'POST' });
}

// ── Completion modal ─────────────────────────────────────────────────────────
function fmtElapsed(ms) {
  const s = Math.floor(ms / 1000);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return m + 'm ' + String(r).padStart(2,'0') + 's';
  return Math.floor(m/60) + 'h ' + String(m%60).padStart(2,'0') + 'm';
}

function showDoneModal() {
  const elapsed = Date.now() - startTime;
  const rate = elapsed > 0 ? (done / (elapsed / 1000)).toFixed(2) : '—';
  $('ds-total').textContent   = done.toLocaleString();
  $('ds-valid').textContent   = (counts.valid   || 0).toLocaleString();
  $('ds-redeemed').textContent= (counts.redeemed|| 0).toLocaleString();
  $('ds-invalid').textContent = (counts.invalid  || 0).toLocaleString();
  $('ds-limited').textContent = (counts.limited  || 0).toLocaleString();
  $('ds-failed').textContent  = (counts.failed   || 0).toLocaleString();
  $('done-time').textContent  = 'Elapsed: ' + fmtElapsed(elapsed);
  $('done-rate').textContent  = rate + ' keys / sec average';
  $('done-modal').classList.add('visible');
}

['done-close','done-dismiss'].forEach(id => {
  document.getElementById(id).addEventListener('click', () => {
    $('done-modal').classList.remove('visible');
  });
});

// ── Lock screen ──────────────────────────────────────────────────────────────
(function() {
  const ls = document.getElementById('lock-screen');
  const inp = document.getElementById('lock-key');
  const btn = document.getElementById('lock-btn');
  const err = document.getElementById('lock-err');

  async function tryUnlock() {
    const key = inp.value.trim();
    if (!key) return;
    btn.disabled = true;
    const r = await fetch('/api/unlock', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key })
    });
    const d = await r.json();
    if (d.ok) {
      ls.classList.add('hidden');
      setTimeout(() => ls.remove(), 420);
      fetch('/api/tokens').then(r => r.json()).then(r => setTokenLabel(r.count));
      renderSorted(); updateStats(); updateCounter();
    } else {
      err.textContent = 'Invalid activation key';
      inp.value = '';
      inp.focus();
    }
    btn.disabled = false;
  }

  btn.addEventListener('click', tryUnlock);
  inp.addEventListener('keydown', e => { if (e.key === 'Enter') tryUnlock(); });
  inp.focus();
})();
</script>
</body>
</html>"""

# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(threaded=True, port=5000)
