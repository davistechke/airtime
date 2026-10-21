# Company Airtime Bulk (Flask + PayHero)

## Run locally
    python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
    pip install -r requirements.txt
    cp .env.example .env     # then fill in your PayHero details
    python app.py            # http://localhost:5000

PayHero must reach `/payhero/callback`, so for testing expose your app with ngrok
and set `PUBLIC_BASE_URL` to the https ngrok URL. (Status polling also works
without the callback.)

## Production
    gunicorn -w 2 -b 0.0.0.0:8000 app:app     # put Nginx + HTTPS in front

## How it works
1. Customer picks network, number and package (10,000 to 100,000 KSh).
2. `/api/order` creates an order and sends an M-Pesa STK push through PayHero.
   Prices are taken from the server-side `PACKAGES` list, never from the browser.
3. On callback or polling, the app confirms with PayHero's transaction-status
   API before marking the order PAID, then sends airtime exactly once.
4. FULFILLMENT=manual: see paid orders at `/admin?token=ADMIN_TOKEN`.
   FULFILLMENT=africastalking: airtime is sent automatically.

Change the bonus in `BONUS_RATE` and the packages in `PACKAGES` (app.py).

## Deploy on Render
1. Push this folder to a GitHub repo (`.env` is git-ignored, so secrets stay out).
2. On render.com choose New > Blueprint, pick the repo; it reads `render.yaml`.
3. Fill in `PAYHERO_AUTH`, `PAYHERO_CHANNEL_ID`, and (after first deploy)
   `PUBLIC_BASE_URL` = your `https://....onrender.com` URL. Redeploy.
4. Read `ADMIN_TOKEN` in the Render dashboard, then open `/admin?token=...`.

The 1 GB disk keeps the orders database safe across deploys. On a free plan
there is no disk, so orders would be wiped on each restart: use the paid plan.
