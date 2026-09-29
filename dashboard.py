from flask import Flask, request, jsonify, Response, send_file
import asyncio
import aiohttp
import json
import threading
import queue
import time
import os
import uuid
from dataclasses import dataclass
from typing import List, Dict

TOKEN_FILE = r"C:\Users\meka3\Desktop\test code\token.txt"
DELAY = 7.5

app = Flask(__name__)

@dataclass
class TokenBucket:
    token: str
    label: str
    last_used: float = 0
    rate_limited_until: float = 0

token_buckets: List[TokenBucket] = []
jobs: Dict[str, queue.Queue] = {}


def load_tokens():
    global token_buckets
    token_buckets = []
    try:
        with open(TOKEN_FILE) as f:
            for i, line in enumerate(f):
                t = line.strip()
                if t:
                    token_buckets.append(TokenBucket(token=t, label=f"Token {i+1}"))
        print(f"Loaded {len(token_buckets)} token(s).", flush=True)
    except FileNotFoundError:
        print("token.txt not found — add Bearer tokens there, one per line.", flush=True)

load_tokens()


async def check_keys_async(keys, result_queue):
    if not token_buckets:
        result_queue.put({"type": "error", "message": "No tokens loaded. Add Bearer tokens to token.txt."})
        result_queue.put({"type": "done"})
        return

    sem = asyncio.Semaphore(max(len(token_buckets) * 2, 5))

    def get_bucket():
        now = time.monotonic()
        available = [b for b in token_buckets if now >= b.rate_limited_until]
        return min(available, key=lambda b: b.last_used) if available else None

    async def check_one(session, key):
        async with sem:
            for attempt in range(3):
                bucket = get_bucket()
                if not bucket:
                    await asyncio.sleep(5)
                    continue

                wait = DELAY - (time.monotonic() - bucket.last_used)
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
                            return key, "Rate Limited", "warning"
                        elif r.status == 401:
                            return key, "Auth Expired", "error"
                        elif r.status == 403:
                            return key, "Wrong Country", "warning"
                        elif r.status == 404:
                            return key, "Invalid", "invalid"
                        elif r.status == 200:
                            data = await r.json()
                            state = data.get("tokenState", "Unknown")
                            if state == "Active":
                                state = "Not Redeemed"
                            return key, state, "valid"
                        else:
                            return key, f"HTTP {r.status}", "error"
                except asyncio.TimeoutError:
                    if attempt < 2:
                        await asyncio.sleep(2)
                        continue
                    return key, "Timeout", "error"
                except Exception as e:
                    if attempt < 2:
                        await asyncio.sleep(2)
                        continue
                    return key, f"Error: {str(e)}", "error"
        return key, "Failed", "error"

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=100)) as session:
        tasks = [check_one(session, k) for k in keys]
        for coro in asyncio.as_completed(tasks):
            key, status, kind = await coro
            result_queue.put({"type": "result", "key": key, "status": status, "kind": kind})

    result_queue.put({"type": "done"})


def run_check_thread(keys, q):
    asyncio.run(check_keys_async(keys, q))


@app.route("/")
def index():
    return send_file(r"C:\Users\meka3\Desktop\test code\index.html")

@app.route("/api/tokens")
def get_tokens():
    return jsonify({"count": len(token_buckets)})

@app.route("/api/reload", methods=["POST"])
def reload():
    load_tokens()
    return jsonify({"count": len(token_buckets)})

@app.route("/api/check", methods=["POST"])
def start_check():
    data = request.json or {}
    keys = [k.strip() for k in data.get("codes", "").splitlines() if k.strip()]
    if not keys:
        return jsonify({"error": "No codes provided"}), 400

    job_id = str(uuid.uuid4())
    q = queue.Queue()
    jobs[job_id] = q
    threading.Thread(target=run_check_thread, args=(keys, q), daemon=True).start()
    return jsonify({"job_id": job_id, "total": len(keys)})

@app.route("/api/stream/<job_id>")
def stream(job_id):
    q = jobs.get(job_id)
    if not q:
        return jsonify({"error": "Job not found"}), 404

    def generate():
        while True:
            try:
                item = q.get(timeout=60)
                yield f"data: {json.dumps(item)}\n\n"
                if item.get("type") == "done":
                    break
            except queue.Empty:
                yield "data: {\"type\":\"keepalive\"}\n\n"
        jobs.pop(job_id, None)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


HTML = """<!-- removed -->
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Key Checker</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
  :root {
    --bg:       #080c12;
    --surface:  #0f1623;
    --surface2: #161e2e;
    --border:   #1e2a3a;
    --fg:       #e2e8f0;
    --muted:    #64748b;
    --accent:   #3b7ff5;
    --accent-d: #2563d4;
    --valid:    #22c55e;
    --invalid:  #ef4444;
    --warning:  #f59e0b;
    --mono: 'JetBrains Mono', monospace;
    --sans: 'Inter', system-ui, sans-serif;
    color-scheme: dark;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: var(--sans);
    background: var(--bg);
    color: var(--fg);
    min-height: 100vh;
    padding: 0 0 40px;
  }

  header {
    border-bottom: 1px solid var(--border);
    padding: 18px 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    background: var(--bg);
    z-index: 10;
  }

  .logo {
    font-size: 15px;
    font-weight: 600;
    letter-spacing: .02em;
    color: var(--fg);
  }

  .token-badge {
    font-size: 12px;
    font-weight: 500;
    padding: 4px 10px;
    border-radius: 20px;
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--muted);
    display: flex;
    align-items: center;
    gap: 7px;
    cursor: pointer;
    transition: border-color .15s;
  }
  .token-badge:hover { border-color: var(--accent); }
  .token-badge .dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    background: var(--muted);
    transition: background .2s;
  }
  .token-badge.active .dot { background: var(--valid); }
  .token-badge.active { color: var(--fg); }

  main {
    max-width: 820px;
    margin: 0 auto;
    padding: 40px 24px 0;
    display: flex;
    flex-direction: column;
    gap: 24px;
  }

  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
  }

  .card-header {
    padding: 16px 20px 14px;
    border-bottom: 1px solid var(--border);
    font-size: 12px;
    font-weight: 600;
    letter-spacing: .08em;
    text-transform: uppercase;
    color: var(--muted);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  textarea {
    width: 100%;
    min-height: 180px;
    background: transparent;
    border: none;
    outline: none;
    resize: vertical;
    padding: 16px 20px;
    font-family: var(--mono);
    font-size: 13px;
    line-height: 1.65;
    color: var(--fg);
    display: block;
  }
  textarea::placeholder { color: var(--muted); }

  .actions {
    padding: 12px 16px;
    border-top: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  .count-hint { font-size: 12px; color: var(--muted); }

  .btn {
    padding: 9px 20px;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
    font-family: var(--sans);
    cursor: pointer;
    border: none;
    transition: background .15s, opacity .15s;
  }
  .btn-primary {
    background: var(--accent);
    color: #fff;
  }
  .btn-primary:hover { background: var(--accent-d); }
  .btn-primary:disabled { opacity: .45; cursor: default; }
  .btn-ghost {
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--muted);
    font-weight: 500;
  }
  .btn-ghost:hover { color: var(--fg); border-color: #2e3d52; }

  .progress-wrap {
    padding: 0 20px 16px;
    display: none;
  }
  .progress-wrap.visible { display: block; }
  .progress-bar-bg {
    height: 3px;
    background: var(--border);
    border-radius: 2px;
    overflow: hidden;
    margin-bottom: 8px;
  }
  .progress-bar-fill {
    height: 100%;
    background: var(--accent);
    border-radius: 2px;
    width: 0%;
    transition: width .3s;
  }
  .progress-label {
    font-size: 11px;
    color: var(--muted);
    font-family: var(--mono);
  }

  .results-list {
    max-height: 440px;
    overflow-y: auto;
    scrollbar-width: thin;
    scrollbar-color: var(--border) transparent;
  }

  .result-row {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 10px 20px;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
    animation: fadeIn .18s ease;
  }
  .result-row:last-child { border-bottom: none; }

  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(4px); }
    to   { opacity: 1; transform: none; }
  }

  .result-key {
    font-family: var(--mono);
    font-size: 12.5px;
    color: var(--fg);
    flex: 1;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .result-status {
    font-size: 11.5px;
    font-weight: 500;
    padding: 3px 9px;
    border-radius: 5px;
    white-space: nowrap;
    flex-shrink: 0;
  }
  .status-valid   { background: #052e16; color: var(--valid); }
  .status-invalid { background: #2d0a0a; color: var(--invalid); }
  .status-warning { background: #2d1f00; color: var(--warning); }
  .status-error   { background: #2d1217; color: #f87171; }

  .empty-state {
    padding: 48px 20px;
    text-align: center;
    color: var(--muted);
    font-size: 13px;
    line-height: 1.7;
  }

  .summary-row {
    display: flex;
    gap: 20px;
    padding: 14px 20px;
    border-top: 1px solid var(--border);
    font-size: 12px;
    color: var(--muted);
    background: var(--surface2);
  }
  .summary-row span b { color: var(--fg); }

  @media (max-width: 540px) {
    header { padding: 14px 16px; }
    main { padding: 24px 12px 0; }
    .result-key { font-size: 11px; }
  }
</style>
</head>
<body>

<header>
  <span class="logo">Key Checker</span>
  <div class="token-badge" id="tokenBadge" onclick="reloadTokens()">
    <span class="dot"></span>
    <span id="tokenText">Loading tokens…</span>
  </div>
</header>

<main>
  <div class="card">
    <div class="card-header">
      <span>Codes</span>
      <button class="btn btn-ghost" style="padding:4px 10px;font-size:11px;" onclick="clearAll()">Clear</button>
    </div>
    <textarea id="codesInput" placeholder="Paste your codes here, one per line&#10;XXXXX-XXXXX-XXXXX-XXXXX-XXXXX"></textarea>
    <div class="progress-wrap" id="progressWrap">
      <div class="progress-bar-bg"><div class="progress-bar-fill" id="progressFill"></div></div>
      <div class="progress-label" id="progressLabel">Checking…</div>
    </div>
    <div class="actions">
      <span class="count-hint" id="countHint">0 codes</span>
      <button class="btn btn-primary" id="checkBtn" onclick="startCheck()">Check Codes</button>
    </div>
  </div>

  <div class="card">
    <div class="card-header">
      <span>Results</span>
      <span id="resultCount" style="font-size:11px;"></span>
    </div>
    <div class="results-list" id="resultsList">
      <div class="empty-state">Results will appear here as codes are checked.</div>
    </div>
    <div class="summary-row" id="summaryRow" style="display:none">
      <span><b id="sumValid">0</b> valid</span>
      <span><b id="sumInvalid">0</b> invalid</span>
      <span><b id="sumOther">0</b> other</span>
    </div>
  </div>
</main>

<script>
  let total = 0, done = 0, valid = 0, invalid = 0, other = 0;
  let checking = false;

  const codesInput  = document.getElementById('codesInput');
  const checkBtn    = document.getElementById('checkBtn');
  const countHint   = document.getElementById('countHint');
  const resultsList = document.getElementById('resultsList');
  const progressWrap= document.getElementById('progressWrap');
  const progressFill= document.getElementById('progressFill');
  const progressLabel=document.getElementById('progressLabel');
  const resultCount = document.getElementById('resultCount');
  const summaryRow  = document.getElementById('summaryRow');
  const tokenBadge  = document.getElementById('tokenBadge');
  const tokenText   = document.getElementById('tokenText');

  codesInput.addEventListener('input', updateCount);
  function updateCount() {
    const n = codesInput.value.split('\n').filter(l => l.trim()).length;
    countHint.textContent = n === 0 ? '0 codes' : `${n} code${n===1?'':'s'}`;
  }

  window.reloadTokens = reloadTokens;
  window.clearAll = clearAll;
  window.startCheck = startCheck;

  async function fetchTokens() {
    const r = await fetch('/api/tokens');
    const d = await r.json();
    if (d.count > 0) {
      tokenText.textContent = `${d.count} token${d.count===1?'':'s'} active`;
      tokenBadge.classList.add('active');
    } else {
      tokenText.textContent = 'No tokens — add to token.txt';
      tokenBadge.classList.remove('active');
    }
  }

  async function reloadTokens() {
    tokenText.textContent = 'Reloading…';
    await fetch('/api/reload', { method: 'POST' });
    await fetchTokens();
  }

  function clearAll() {
    codesInput.value = '';
    updateCount();
    resultsList.innerHTML = '<div class="empty-state">Results will appear here as codes are checked.</div>';
    summaryRow.style.display = 'none';
    resultCount.textContent = '';
    progressWrap.classList.remove('visible');
  }

  async function startCheck() {
    if (checking) return;
    const codes = codesInput.value.trim();
    if (!codes) return;

    checking = true;
    checkBtn.disabled = true;
    checkBtn.textContent = 'Checking…';
    done = 0; valid = 0; invalid = 0; other = 0;
    resultsList.innerHTML = '';
    summaryRow.style.display = 'none';
    progressWrap.classList.add('visible');
    progressFill.style.width = '0%';

    const res = await fetch('/api/check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ codes })
    });
    const { job_id, total: t, error } = await res.json();

    if (error) {
      resultsList.innerHTML = `<div class="empty-state" style="color:#f87171">${error}</div>`;
      resetBtn(); return;
    }

    total = t;
    progressLabel.textContent = `0 / ${total} checked`;
    resultCount.textContent = `0 / ${total}`;

    const es = new EventSource(`/api/stream/${job_id}`);
    es.onmessage = e => {
      const msg = JSON.parse(e.data);
      if (msg.type === 'keepalive') return;
      if (msg.type === 'error') {
        resultsList.innerHTML = `<div class="empty-state" style="color:#f87171">${msg.message}</div>`;
        es.close(); resetBtn(); return;
      }
      if (msg.type === 'result') {
        done++;
        const kind = msg.kind;
        if (kind === 'valid') valid++;
        else if (kind === 'invalid') invalid++;
        else other++;

        const pct = Math.round(done / total * 100);
        progressFill.style.width = pct + '%';
        progressLabel.textContent = `${done} / ${total} checked`;
        resultCount.textContent = `${done} / ${total}`;

        const row = document.createElement('div');
        row.className = 'result-row';
        const cls = kind === 'valid' ? 'status-valid' : kind === 'invalid' ? 'status-invalid' : kind === 'warning' ? 'status-warning' : 'status-error';
        row.innerHTML = `<span class="result-key">${msg.key}</span><span class="result-status ${cls}">${msg.status}</span>`;
        resultsList.appendChild(row);
        row.scrollIntoView({ block: 'nearest' });

        document.getElementById('sumValid').textContent = valid;
        document.getElementById('sumInvalid').textContent = invalid;
        document.getElementById('sumOther').textContent = other;
        summaryRow.style.display = 'flex';
      }
      if (msg.type === 'done') {
        es.close();
        progressLabel.textContent = `Done — ${total} codes checked`;
        resetBtn();
      }
    };
    es.onerror = () => { es.close(); resetBtn(); };
  }

  function resetBtn() {
    checking = false;
    checkBtn.disabled = false;
    checkBtn.textContent = 'Check Codes';
  }

  fetchTokens();
  updateCount();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print("Dashboard: http://localhost:5000", flush=True)
    app.run(debug=False, threaded=True, port=5000)
