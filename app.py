import os
import time
import hashlib
import logging
import requests
from flask import Flask, request, jsonify

# ─── Configure Logging ────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ─── Environment Variables (Required) ─────────────────────────────────────────
WEBHOOK_PASSPHRASE = os.environ.get("WEBHOOK_PASSPHRASE")
if not WEBHOOK_PASSPHRASE:
    raise RuntimeError("WEBHOOK_PASSPHRASE env var is required")

TRADERSPOST_GOLD_WEBHOOK_URL = os.environ.get("TRADERSPOST_GOLD_WEBHOOK_URL")
TRADERSPOST_MES_WEBHOOK_URL = os.environ.get("TRADERSPOST_MES_WEBHOOK_URL")

# ─── Environment Variables (Configurable with defaults) ───────────────────────
MGC_CONTRACT = os.environ.get("MGC_CONTRACT", "MGCM2026")
MES_CONTRACT = os.environ.get("MES_CONTRACT", "MESM2026")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Position sizing — configurable via env vars, no redeploy needed
GOLD_QUANTITY = int(os.environ.get("GOLD_QUANTITY", "5"))
MES_QUANTITY = int(os.environ.get("MES_QUANTITY", "10"))

# TP/SL total dollar amounts — per-contract values are calculated from these
GOLD_TP_TOTAL = float(os.environ.get("GOLD_TP_TOTAL", "350"))
GOLD_SL_TOTAL = float(os.environ.get("GOLD_SL_TOTAL", "888"))
MES_TP_TOTAL = float(os.environ.get("MES_TP_TOTAL", "350"))
MES_SL_TOTAL = float(os.environ.get("MES_SL_TOTAL", "888"))

# Dollars per point — used to convert dollar amounts to points for TradersPost
GOLD_DOLLAR_PER_POINT = float(os.environ.get("GOLD_DOLLAR_PER_POINT", "10"))
MES_DOLLAR_PER_POINT = float(os.environ.get("MES_DOLLAR_PER_POINT", "5"))

# ─── Duplicate Signal Protection ──────────────────────────────────────────────
# In-memory cache to prevent duplicate trades from webhook retries
# Key: hash of signal+price, Value: timestamp
SIGNAL_CACHE = {}
SIGNAL_CACHE_TTL = 60  # seconds — ignore duplicate signals within this window


def is_duplicate_signal(signal, price):
    """Check if this exact signal was already processed recently."""
    cache_key = hashlib.md5(f"{signal}:{round(float(price), 1)}".encode()).hexdigest()
    now = time.time()

    # Clean expired entries
    expired_keys = [k for k, v in SIGNAL_CACHE.items() if now - v > SIGNAL_CACHE_TTL]
    for k in expired_keys:
        del SIGNAL_CACHE[k]

    if cache_key in SIGNAL_CACHE:
        return True

    SIGNAL_CACHE[cache_key] = now
    return False


# ─── Legacy Gold Signals (unprefixed — from existing Pine Scripts) ────────────
LEGACY_GOLD_SIGNALS = {
    "NPR_BULL_ELEPHANT", "NPR_BEAR_ELEPHANT",
    "NPR_BULL_TAIL", "NPR_BEAR_TAIL",
    "NPR_BULL_180", "NPR_BEAR_180",
    "TREND_REVERSE_BULL", "TREND_REVERSE_BEAR",
}

# ─── All Valid Signals ────────────────────────────────────────────────────────
VALID_SIGNALS = {
    # Gold signals (GOLD_ prefix)
    "GOLD_BULLISH_180", "GOLD_BEARISH_180",
    "GOLD_BULLISH_LIQUIDITY_RUN", "GOLD_BEARISH_LIQUIDITY_RUN",
    "GOLD_TREND_REVERSE_BULL", "GOLD_TREND_REVERSE_BEAR",
    "GOLD_NPR_BULL_ELEPHANT", "GOLD_NPR_BEAR_ELEPHANT",
    "GOLD_NPR_BULL_TAIL", "GOLD_NPR_BEAR_TAIL",
    "GOLD_NPR_BULL_180", "GOLD_NPR_BEAR_180",
    # MES signals (MES_ prefix)
    "MES_BULLISH_180", "MES_BEARISH_180",
    "MES_BULLISH_LIQUIDITY_RUN", "MES_BEARISH_LIQUIDITY_RUN",
    "MES_TREND_REVERSE_BULL", "MES_TREND_REVERSE_BEAR",
    "MES_NPR_BULL_ELEPHANT", "MES_NPR_BEAR_ELEPHANT",
    "MES_NPR_BULL_TAIL", "MES_NPR_BEAR_TAIL",
    "MES_NPR_BULL_180", "MES_NPR_BEAR_180",
} | LEGACY_GOLD_SIGNALS


# ─── Helper Functions ─────────────────────────────────────────────────────────

def send_telegram_message(message):
    """Sends a message to Telegram. Non-blocking — errors are logged but never stop execution."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info("Telegram credentials not set. Skipping notification.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        response = requests.post(url, json=payload, timeout=5)
        if response.status_code != 200:
            logger.error(f"Failed to send Telegram message: {response.text}")
    except Exception as e:
        logger.error(f"Exception sending Telegram message: {e}")


def determine_instrument(signal):
    """
    Explicit routing:
    - MES_ prefix → MES
    - GOLD_ prefix → Gold
    - Known legacy unprefixed signals → Gold
    - Anything else → error (never silently route)
    """
    if signal.startswith("MES_"):
        return "MES"
    elif signal.startswith("GOLD_") or signal in LEGACY_GOLD_SIGNALS:
        return "GOLD"
    else:
        return None  # Unknown — will be caught by validation


def determine_direction(signal):
    """
    Direction detection:
    - BULL or BULLISH in signal → buy (LONG)
    - BEAR or BEARISH in signal → sell (SHORT)
    """
    signal_upper = signal.upper()
    if "BULL" in signal_upper:
        return "buy"
    elif "BEAR" in signal_upper:
        return "sell"
    return None


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "message": "Trading Bot v2 is running"}), 200


@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Invalid JSON payload"}), 400

        # ── Step 1: Validate passphrase ───────────────────────────────────────
        passphrase = data.get("passphrase")
        if passphrase != WEBHOOK_PASSPHRASE:
            logger.warning("Unauthorized webhook attempt.")
            return jsonify({"error": "Unauthorized"}), 401

        signal = data.get("signal", "").strip()

        # Price extraction — accept "price" or "entry" field (NPR scripts use "entry")
        raw_price = data.get("price") or data.get("entry") or 0
        try:
            price = round(float(raw_price), 2)
        except (ValueError, TypeError):
            logger.error(f"Invalid price value: {raw_price}")
            return jsonify({"error": f"Invalid price value: {raw_price}"}), 400

        if price <= 0:
            logger.error(f"Price must be positive, got: {price}")
            return jsonify({"error": "Price must be positive"}), 400

        if not signal:
            return jsonify({"error": "Missing signal"}), 400

        # ── Step 2: Validate signal name ──────────────────────────────────────
        if signal not in VALID_SIGNALS:
            logger.warning(f"Unknown signal: {signal}")
            return jsonify({"error": f"Unknown signal: {signal}"}), 400

        # ── Step 3: Duplicate protection ──────────────────────────────────────
        if is_duplicate_signal(signal, price):
            logger.warning(f"Duplicate signal ignored: {signal} at price {price}")
            return jsonify({"status": "duplicate", "message": "Signal already processed"}), 200

        logger.info(f"Received valid signal: {signal} at price {price}")

        # ── Step 4: Determine instrument ──────────────────────────────────────
        instrument = determine_instrument(signal)
        if not instrument:
            logger.error(f"Cannot determine instrument for signal: {signal}")
            return jsonify({"error": "Cannot determine instrument"}), 400

        # ── Step 5: Set parameters based on instrument ────────────────────────
        if instrument == "GOLD":
            ticker = MGC_CONTRACT
            quantity = GOLD_QUANTITY
            tp_dollars_per_contract = round(GOLD_TP_TOTAL / GOLD_QUANTITY, 2)
            sl_dollars_per_contract = round(GOLD_SL_TOTAL / GOLD_QUANTITY, 2)
            tp_points = round(tp_dollars_per_contract / GOLD_DOLLAR_PER_POINT, 2)
            sl_points = round(sl_dollars_per_contract / GOLD_DOLLAR_PER_POINT, 2)
            webhook_url = TRADERSPOST_GOLD_WEBHOOK_URL
            instrument_label = f"Gold (MGC) - {ticker}"
        else:  # MES
            ticker = MES_CONTRACT
            quantity = MES_QUANTITY
            tp_dollars_per_contract = round(MES_TP_TOTAL / MES_QUANTITY, 2)
            sl_dollars_per_contract = round(MES_SL_TOTAL / MES_QUANTITY, 2)
            tp_points = round(tp_dollars_per_contract / MES_DOLLAR_PER_POINT, 2)
            sl_points = round(sl_dollars_per_contract / MES_DOLLAR_PER_POINT, 2)
            webhook_url = TRADERSPOST_MES_WEBHOOK_URL
            instrument_label = f"MES - {ticker}"

        # ── Step 6: Determine direction ───────────────────────────────────────
        action = determine_direction(signal)
        if not action:
            logger.error(f"Could not determine direction for signal: {signal}")
            return jsonify({"error": "Could not determine direction"}), 400

        # ── Step 7: Construct TradersPost payload ─────────────────────────────
        tp_payload = {
            "ticker": ticker,
            "action": action,
            "price": price,
            "quantity": quantity,
            "takeProfit": {
                "amount": tp_points
            },
            "stopLoss": {
                "amount": sl_points
            }
        }

        logger.info(f"Forwarding to TradersPost ({instrument_label}): {tp_payload}")

        # ── Step 8: Forward to TradersPost ────────────────────────────────────
        trade_success = False
        tp_error_msg = ""

        if webhook_url:
            try:
                tp_response = requests.post(webhook_url, json=tp_payload, timeout=10)
                logger.info(f"TradersPost response: {tp_response.status_code} - {tp_response.text}")

                if tp_response.ok:
                    trade_success = True
                else:
                    tp_error_msg = f"TradersPost returned {tp_response.status_code}: {tp_response.text}"
                    logger.error(tp_error_msg)
            except Exception as e:
                tp_error_msg = f"Error forwarding to TradersPost: {e}"
                logger.error(tp_error_msg)
        else:
            tp_error_msg = f"TradersPost webhook URL not configured for {instrument}"
            logger.warning(tp_error_msg)

        # ── Step 9: Send Telegram notification ────────────────────────────────
        if trade_success:
            direction_emoji = "🟢" if action == "buy" else "🔴"
            msg = (
                f"<b>{direction_emoji} Trade Entry Alert</b>\n\n"
                f"<b>Signal:</b> {signal}\n"
                f"<b>Instrument:</b> {instrument_label}\n"
                f"<b>Action:</b> {action.upper()}\n"
                f"<b>Quantity:</b> {quantity}\n"
                f"<b>Price:</b> {price}\n"
                f"<b>TP:</b> {tp_points} pts/contract (${tp_dollars_per_contract:.2f}/contract, ${tp_dollars_per_contract * quantity:.2f} total)\n"
                f"<b>SL:</b> {sl_points} pts/contract (${sl_dollars_per_contract:.2f}/contract, ${sl_dollars_per_contract * quantity:.2f} total)"
            )
            send_telegram_message(msg)
            return jsonify({"status": "success", "message": f"Signal {signal} processed for {instrument}"}), 200
        else:
            # Trade failed — alert via Telegram and return error
            error_msg = (
                f"<b>⚠️ TRADE FAILED</b>\n\n"
                f"<b>Signal:</b> {signal}\n"
                f"<b>Instrument:</b> {instrument_label}\n"
                f"<b>Error:</b> {tp_error_msg}"
            )
            send_telegram_message(error_msg)
            return jsonify({"error": "Trade forwarding failed", "details": tp_error_msg}), 502

    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        return jsonify({"error": "Internal server error"}), 500


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
