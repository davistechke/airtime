"""Company Airtime Bulk - Flask app with PayHero (M-Pesa STK Push) payments."""
import json, os, re, secrets, sqlite3, time
from contextlib import closing

import requests
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, render_template, request

load_dotenv()
app = Flask(__name__)

BONUS_RATE = 0.5                                   # 50% extra airtime
PACKAGES = [10000, 20000, 30000, 50000, 75000, 100000]   # amount PAID (KSh)
NETWORKS = ["Safaricom", "Airtel", "Telkom"]

PAYHERO_URL = "https://backend.payhero.co.ke/api/v2"
PAYHERO_AUTH = os.getenv("PAYHERO_AUTH", "")
CHANNEL_ID = os.getenv("PAYHERO_CHANNEL_ID", "")
BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
FULFILLMENT = os.getenv("FULFILLMENT", "manual")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
DB = os.getenv("DB_PATH", "orders.db")


# ---------- database ----------
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with closing(db()) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS orders(
            ref TEXT PRIMARY KEY, network TEXT, recipient TEXT, payer TEXT,
            paid INTEGER, airtime INTEGER,
            status TEXT DEFAULT 'PENDING',      -- PENDING|PAID|FAILED|DELIVERED
            mpesa_receipt TEXT, payhero_ref TEXT, note TEXT, created REAL)""")
        c.commit()


init_db()


# ---------- helpers ----------
def norm_phone(raw):
    """Return 07XXXXXXXX / 01XXXXXXXX or None."""
    d = re.sub(r"\D", "", raw or "")
    if d.startswith("254"):
        d = "0" + d[3:]
    return d if re.fullmatch(r"0[17]\d{8}", d) else None


def intl(p):  # 0712345678 -> 254712345678
    return "254" + p[1:]


def airtime_for(paid):
    return int(paid * (1 + BONUS_RATE))


def payhero_status(ref):
    """Ask PayHero directly for the truth (never trust a callback blindly)."""
    r = requests.get(f"{PAYHERO_URL}/transaction-status", params={"reference": ref},
                     headers={"Authorization": PAYHERO_AUTH}, timeout=20)
    r.raise_for_status()
    return r.json()


def fulfil(ref):
    """Send airtime once. Safe to call repeatedly."""
    with closing(db()) as c:
        # atomically claim the order so airtime can never be sent twice
        cur = c.execute("UPDATE orders SET status='PROCESSING' WHERE ref=? AND status='PAID'", (ref,))
        c.commit()
        if cur.rowcount == 0:
            return
        o = c.execute("SELECT * FROM orders WHERE ref=?", (ref,)).fetchone()

    if FULFILLMENT != "africastalking":
        new, note = "PAID", "Awaiting manual top-up"
    else:
        try:
            r = requests.post(
                "https://api.africastalking.com/version1/airtime/send",
                headers={"apiKey": os.getenv("AT_API_KEY", ""), "Accept": "application/json"},
                data={"username": os.getenv("AT_USERNAME", ""),
                      "recipients": json.dumps([{"phoneNumber": "+" + intl(o["recipient"]),
                                                 "amount": f"KES {o['airtime']}"}])},
                timeout=30)
            data = r.json()
            ok = r.status_code in (200, 201) and int(data.get("numSent", 0)) >= 1
            new, note = ("DELIVERED", "Sent via Africa's Talking") if ok else ("PAID", f"AT error: {data}")
        except Exception as e:  # leave as PAID so it can be retried from /admin
            new, note = "PAID", f"AT exception: {e}"
    with closing(db()) as c:
        c.execute("UPDATE orders SET status=?, note=? WHERE ref=?", (new, note, ref))
        c.commit()


def settle(ref):
    """Verify with PayHero, then mark PAID/FAILED and fulfil."""
    with closing(db()) as c:
        o = c.execute("SELECT * FROM orders WHERE ref=?", (ref,)).fetchone()
    if not o or o["status"] != "PENDING":
        return
    try:
        data = payhero_status(ref)
    except Exception:
        return
    st = str(data.get("status", "")).upper()
    amt = float(data.get("amount", o["paid"]) or o["paid"])
    if st == "SUCCESS" and amt >= o["paid"]:
        with closing(db()) as c:
            c.execute("UPDATE orders SET status='PAID', mpesa_receipt=? WHERE ref=? AND status='PENDING'",
                      (data.get("provider_reference") or data.get("MpesaReceiptNumber"), ref))
            c.commit()
        fulfil(ref)
    elif st in ("FAILED", "CANCELLED"):
        with closing(db()) as c:
            c.execute("UPDATE orders SET status='FAILED', note=? WHERE ref=? AND status='PENDING'",
                      (data.get("error_message") or st, ref))
            c.commit()


# ---------- routes ----------
@app.get("/")
def index():
    return render_template("index.html", packages=PACKAGES, networks=NETWORKS,
                           bonus=int(BONUS_RATE * 100))


@app.post("/api/order")
def create_order():
    j = request.get_json(silent=True) or {}
    network, paid = j.get("network"), j.get("amount")
    recipient = norm_phone(j.get("recipient"))
    payer = norm_phone(j.get("payer")) or recipient
    if network not in NETWORKS or paid not in PACKAGES or not recipient or not payer:
        return jsonify(error="Invalid network, package or phone number."), 400

    ref = "CAB-" + secrets.token_hex(5).upper()
    with closing(db()) as c:
        c.execute("INSERT INTO orders(ref,network,recipient,payer,paid,airtime,created) VALUES(?,?,?,?,?,?,?)",
                  (ref, network, recipient, payer, paid, airtime_for(paid), time.time()))
        c.commit()
    try:
        r = requests.post(f"{PAYHERO_URL}/payments", timeout=25,
                          headers={"Authorization": PAYHERO_AUTH, "Content-Type": "application/json"},
                          json={"amount": paid, "phone_number": payer,
                                "channel_id": int(CHANNEL_ID), "provider": "m-pesa",
                                "external_reference": ref, "customer_name": "Company Airtime Bulk",
                                "callback_url": f"{BASE_URL}/payhero/callback"})
        data = r.json()
        if r.status_code >= 400 or not data.get("success", True):
            raise RuntimeError(data)
        with closing(db()) as c:
            c.execute("UPDATE orders SET payhero_ref=? WHERE ref=?", (data.get("reference"), ref))
            c.commit()
    except Exception as e:
        app.logger.error("PayHero error: %s", e)
        with closing(db()) as c:
            c.execute("UPDATE orders SET status='FAILED', note='Could not start payment' WHERE ref=?", (ref,))
            c.commit()
        return jsonify(error="Could not send the M-Pesa prompt. Please try again."), 502
    return jsonify(ref=ref)


@app.post("/payhero/callback")
def callback():
    j = request.get_json(silent=True) or {}
    resp = j.get("response", j)
    ref = resp.get("ExternalReference")
    if ref:
        settle(ref)          # verifies with PayHero before releasing airtime
    return jsonify(ok=True)


@app.get("/api/status/<ref>")
def status(ref):
    settle(ref)              # fallback if the callback is delayed
    with closing(db()) as c:
        o = c.execute("SELECT status, airtime, network, recipient, note FROM orders WHERE ref=?", (ref,)).fetchone()
    if not o:
        abort(404)
    return jsonify(dict(o))


@app.get("/admin")
def admin():
    if not ADMIN_TOKEN or request.args.get("token") != ADMIN_TOKEN:
        abort(403)
    with closing(db()) as c:
        rows = c.execute("SELECT * FROM orders ORDER BY created DESC LIMIT 200").fetchall()
    return render_template("admin.html", rows=rows)


@app.post("/admin/delivered/<ref>")
def mark_delivered(ref):
    if not ADMIN_TOKEN or request.args.get("token") != ADMIN_TOKEN:
        abort(403)
    with closing(db()) as c:
        c.execute("UPDATE orders SET status='DELIVERED', note='Topped up manually' WHERE ref=? AND status='PAID'", (ref,))
        c.commit()
    return ("", 204)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
