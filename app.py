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
TRADERSPOST_MES_WEBHOOK_URL  = os.environ.get("TRADERSPOST_MES_WEBHOOK_URL")

# ─── Environment Variables (Configurable with defaults) ───────────────────────
MGC_CONTRACT       = os.environ.get("MGC_CONTRACT",  "MGCM2026")
MES_CONTRACT       = os.environ.get("MES_CONTRACT",  "MESM2026")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")

# Position sizing
GOLD_QUANTITY = int(os.environ.get("GOLD_QUANTITY", "5"))
MES_QUANTITY  = int(os.environ.get("MES_QUANTITY",  "10"))

# TP/SL total dollar amounts
GOLD_TP_TOTAL = float(os.environ.get("GOLD_TP_TOTAL", "350"))
GOLD_SL_TOTAL = float(os.environ.get("GOLD_SL_TOTAL", "888"))
MES_TP_TOTAL  = float(os.environ.get("MES_TP_TOTAL",  "350"))
MES_SL_TOTAL  = float(os.environ.get("MES_SL_TOTAL",  "888"))

# Dollars per point
GOLD_DOLLAR_PER_POINT = float(os.environ.get("GOLD_DOLLAR_PER_POINT", "10"))
MES_DOLLAR_PER_POINT  = float(os.environ.get("MES_DOLLAR_PER_POINT",  "5"))

# ─── Contrarian Mode ──────────────────────────────────────────────────────────
# When True, every BULL signal → sell and every BEAR signal → buy.
# Toggle in Railway env vars — no redeploy needed (restart required).
CONTRARIAN_MODE = os.environ.get("CONTRARIAN_MODE", "false").lower() == "true"
if CONTRARIAN_MODE:
    logger.info("⚠️  CONTRARIAN MODE ENABLED — all signal directions will be reversed")

# ─── Trade Duration Expiry ────────────────────────────────────────────────────
# After this many minutes the bot assumes the trade has closed naturally
# (hit TP, SL, or was manually exited) and will accept new signals again.
# Change via env var — no redeploy needed.
MAX_TRADE_DURATION_SECONDS = int(os.environ.get("MAX_TRADE_DURATION_MINUTES", "120")) * 60

# ─── Duplicate Signal Protection ──────────────────────────────────────────────
SIGNAL_CACHE     = {}
SIGNAL_CACHE_TTL = 60  # seconds


def is_duplicate_signal(signal, price):
    """Return True if this signal+price was already processed within the TTL window."""
    cache_key = hashlib.md5(f"{signal}:{round(float(price), 1)}".encode()).hexdigest()
    now = time.time()
    expired = [k for k, v in SIGNAL_CACHE.items() if now - v > SIGNAL_CACHE_TTL]
    for k in expired:
        del SIGNAL_CACHE[k]
    if cache_key in SIGNAL_CACHE:
        return True
    SIGNAL_CACHE[cache_key] = now
    return False


# ─── Position State Tracking ──────────────────────────────────────────────────
# Tracks open trades per instrument so only one trade runs at a time.
# ALL incoming signals (same or opposing direction) are ignored while a
# trade is open. The trade closes naturally via its TP or SL at the broker.
#
# State auto-expires after MAX_TRADE_DURATION_MINUTES.
# If the bot restarts mid-trade, use POST /close to manually resync.
OPEN_POSITIONS = {}


def has_open_position(instrument):
    """
    Returns True if a non-expired trade is recorded as open.
    Auto-clears and returns False if the position has exceeded MAX_TRADE_DURATION_SECONDS.
    """
    pos = OPEN_POSITIONS.get(instrument)
    if not pos:
        return False

    elapsed = time.time() - pos["opened_at"]
    if elapsed > MAX_TRADE_DURATION_SECONDS:
        logger.info(
            f"Position on {instrument} auto-expired after {int(elapsed // 60)} min "
            f"(max={MAX_TRADE_DURATION_SECONDS // 60} min). Assuming trade closed naturally."
        )
        send_telegram_message(
            f"\u23f1 <b>Position Auto-Expired</b>\n\n"
            f"<b>Instrument:</b> {instrument}\n"
            f"<b>Was:</b> {pos['direction'].upper()} @ {pos['price']}\n"
            f"<b>Signal:</b> {pos['signal']}\n"
            f"<b>Open for:</b> {int(elapsed // 60)} min\n"
            f"Bot will now accept new signals for {instrument}."
        )
        clear_open_position(instrument)
        return False

    return True


def get_open_position(instrument):
    return OPEN_POSITIONS.get(instrument)


def set_open_position(instrument, direction, signal, price):
    now = time.time()
    OPEN_POSITIONS[instrument] = {
        "direction":      direction,
        "signal":         signal,
        "price":          price,
        "opened_at":      now,
        "opened_at_str":  time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now)),
        "expires_at_str": time.strftime(
            "%Y-%m-%d %H:%M:%S UTC",
            time.gmtime(now + MAX_TRADE_DURATION_SECONDS)
        ),
    }
    logger.info(
        f"Position opened: {instrument} {direction.upper()} @ {price} "
        f"({signal}) — auto-expires in {MAX_TRADE_DURATION_SECONDS // 60} min"
    )


def clear_open_position(instrument):
    if instrument in OPEN_POSITIONS:
        pos = OPEN_POSITIONS.pop(instrument)
        logger.info(
            f"Position cleared: {instrument} was "
            f"{pos['direction'].upper()} @ {pos['price']} ({pos['signal']})"
        )


# ─── Signal Definitions ───────────────────────────────────────────────────────

LEGACY_GOLD_SIGNALS = {
    "NPR_BULL_ELEPHANT",  "NPR_BEAR_ELEPHANT",
    "NPR_BULL_TAIL",      "NPR_BEAR_TAIL",
    "NPR_BULL_180",       "NPR_BEAR_180",
    "NPR_BULL",           "NPR_BEAR",
    "TREND_REVERSE_BULL", "TREND_REVERSE_BEAR",
}

VALID_SIGNALS = {
    # Gold signals (GOLD_ prefix)
    "GOLD_BULLISH_180",           "GOLD_BEARISH_180",
    "GOLD_BULLISH_LIQUIDITY_RUN", "GOLD_BEARISH_LIQUIDITY_RUN",
    "GOLD_TREND_REVERSE_BULL",    "GOLD_TREND_REVERSE_BEAR",
    "GOLD_NPR_BULL_ELEPHANT",     "GOLD_NPR_BEAR_ELEPHANT",
    "GOLD_NPR_BULL_TAIL",         "GOLD_NPR_BEAR_TAIL",
    "GOLD_NPR_BULL_180",          "GOLD_NPR_BEAR_180",
    # MES signals (MES_ prefix)
    "MES_BULLISH_180",            "MES_BEARISH_180",
    "MES_BULLISH_LIQUIDITY_RUN",  "MES_BEARISH_LIQUIDITY_RUN",
    "MES_TREND_REVERSE_BULL",     "MES_TREND_REVERSE_BEAR",
    "MES_NPR_BULL_ELEPHANT",      "MES_NPR_BEAR_ELEPHANT",
    "MES_NPR_BULL_TAIL",          "MES_NPR_BEAR_TAIL",
    "MES_NPR_BULL_180",           "MES_NPR_BEAR_180",
} | LEGACY_GOLD_SIGNALS


# ─── Helper Functions ─────────────────────────────────────────────────────────

def send_telegram_message(message):
    """Send a Telegram message. Non-blocking — errors are logged, never fatal."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info("Telegram credentials not set — skipping notification.")
        return
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=5)
        if resp.status_code != 200:
            logger.error(f"Telegram error: {resp.text}")
    except Exception as e:
        logger.error(f"Telegram exception: {e}")


def determine_instrument(signal):
    if signal.startswith("MES_"):
        return "MES"
    if signal.startswith("GOLD_") or signal in LEGACY_GOLD_SIGNALS:
        return "GOLD"
    return None


def determine_direction(signal):
    su = signal.upper()
    if "BULL" in su:
        direction = "buy"
    elif "BEAR" in su:
        direction = "sell"
    else:
        return None

    if CONTRARIAN_MODE:
        direction = "sell" if direction == "buy" else "buy"
        logger.info(f"Contrarian mode: {signal} direction reversed → {direction.upper()}")

    return direction


def build_instrument_params(instrument):
    """Return a dict of all per-instrument parameters."""
    if instrument == "GOLD":
        quantity                = GOLD_QUANTITY
        tp_dollars_per_contract = round(GOLD_TP_TOTAL / GOLD_QUANTITY, 2)
        sl_dollars_per_contract = round(GOLD_SL_TOTAL / GOLD_QUANTITY, 2)
        tp_points               = round(tp_dollars_per_contract / GOLD_DOLLAR_PER_POINT, 2)
        sl_points               = round(sl_dollars_per_contract / GOLD_DOLLAR_PER_POINT, 2)
        return {
            "ticker":      MGC_CONTRACT,
            "quantity":    quantity,
            "tp_points":   tp_points,
            "sl_points":   sl_points,
            "tp_per_ctr":  tp_dollars_per_contract,
            "sl_per_ctr":  sl_dollars_per_contract,
            "webhook_url": TRADERSPOST_GOLD_WEBHOOK_URL,
            "label":       f"Gold (MGC) - {MGC_CONTRACT}",
        }
    else:  # MES
        quantity                = MES_QUANTITY
        tp_dollars_per_contract = round(MES_TP_TOTAL / MES_QUANTITY, 2)
        sl_dollars_per_contract = round(MES_SL_TOTAL / MES_QUANTITY, 2)
        tp_points               = round(tp_dollars_per_contract / MES_DOLLAR_PER_POINT, 2)
        sl_points               = round(sl_dollars_per_contract / MES_DOLLAR_PER_POINT, 2)
        return {
            "ticker":      MES_CONTRACT,
            "quantity":    quantity,
            "tp_points":   tp_points,
            "sl_points":   sl_points,
            "tp_per_ctr":  tp_dollars_per_contract,
            "sl_per_ctr":  sl_dollars_per_contract,
            "webhook_url": TRADERSPOST_MES_WEBHOOK_URL,
            "label":       f"MES - {MES_CONTRACT}",
        }


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/', methods=['GET'])
def health_check():
    return jsonify({
        "status":         "healthy",
        "message":        "Trading Bot is running",
        "contrarian_mode": CONTRARIAN_MODE,
        "open_positions": OPEN_POSITIONS,
        "max_trade_duration_minutes": MAX_TRADE_DURATION_SECONDS // 60,
    }), 200


@app.route('/positions', methods=['GET'])
def get_positions():
    """View current open position state and time remaining before auto-expiry."""
    now = time.time()
    enriched = {}
    for instrument, pos in OPEN_POSITIONS.items():
        elapsed   = now - pos["opened_at"]
        remaining = max(0, MAX_TRADE_DURATION_SECONDS - elapsed)
        enriched[instrument] = {
            **pos,
            "open_for_minutes":   round(elapsed / 60, 1),
            "expires_in_minutes": round(remaining / 60, 1),
        }
    return jsonify({
        "open_positions": enriched,
        "count":          len(enriched),
    }), 200


@app.route('/close', methods=['POST'])
def close_position():
    """
    Manually clear a position from bot state.
    Use this to resync after a bot restart that happened mid-trade,
    or after manually closing a trade at the broker.

    Body: { "passphrase": "...", "instrument": "GOLD" or "MES" }
    """
    data = request.get_json()
    if not data or data.get("passphrase") != WEBHOOK_PASSPHRASE:
        return jsonify({"error": "Unauthorized"}), 401

    instrument = data.get("instrument", "").upper()
    if instrument not in ("GOLD", "MES"):
        return jsonify({"error": "instrument must be GOLD or MES"}), 400

    if not has_open_position(instrument):
        return jsonify({
            "status":  "no_position",
            "message": f"No open position recorded for {instrument}",
        }), 200

    pos = get_open_position(instrument)
    clear_open_position(instrument)

    send_telegram_message(
        f"\u2705 <b>Position Manually Cleared</b>\n\n"
        f"<b>Instrument:</b> {instrument}\n"
        f"<b>Was:</b> {pos['direction'].upper()} @ {pos['price']}\n"
        f"<b>Signal:</b> {pos['signal']}\n"
        f"<b>Opened:</b> {pos['opened_at_str']}\n\n"
        f"Bot will now accept new signals for {instrument}."
    )

    return jsonify({
        "status":     "cleared",
        "instrument": instrument,
        "was":        pos,
    }), 200


@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Invalid JSON payload"}), 400

        # ── 1. Validate passphrase ────────────────────────────────────────────
        if data.get("passphrase") != WEBHOOK_PASSPHRASE:
            logger.warning("Unauthorized webhook attempt.")
            return jsonify({"error": "Unauthorized"}), 401

        signal = data.get("signal", "").strip()

        # ── 2. Extract price — accept "price" OR "entry" ──────────────────────
        raw_price = data.get("price") or data.get("entry") or 0
        try:
            price = round(float(raw_price), 2)
        except (ValueError, TypeError):
            logger.error(f"Invalid price value: {raw_price}")
            return jsonify({"error": f"Invalid price value: {raw_price}"}), 400

        if price <= 0:
            return jsonify({"error": "Price must be positive"}), 400

        if not signal:
            return jsonify({"error": "Missing signal"}), 400

        # ── 3. Validate signal name ───────────────────────────────────────────
        if signal not in VALID_SIGNALS:
            logger.warning(f"Unknown signal received: '{signal}'")
            return jsonify({"error": f"Unknown signal: {signal}"}), 400

        # ── 4. Duplicate protection ───────────────────────────────────────────
        if is_duplicate_signal(signal, price):
            logger.warning(f"Duplicate signal ignored: {signal} @ {price}")
            return jsonify({"status": "duplicate", "message": "Signal already processed"}), 200

        logger.info(f"Valid signal received: {signal} @ {price}")

        # ── 5. Determine instrument and direction ─────────────────────────────
        instrument = determine_instrument(signal)
        if not instrument:
            return jsonify({"error": "Cannot determine instrument"}), 400

        action = determine_direction(signal)
        if not action:
            return jsonify({"error": "Cannot determine direction"}), 400

        # ── 6. Build instrument parameters ────────────────────────────────────
        params = build_instrument_params(instrument)

        # ── 7. Position guard — block ALL signals while trade is open ─────────
        # has_open_position() auto-expires stale positions before checking.
        # Same AND opposing signals are both ignored — let TP/SL work naturally.
        if has_open_position(instrument):
            open_pos  = get_open_position(instrument)
            open_dir  = open_pos["direction"]
            elapsed   = round((time.time() - open_pos["opened_at"]) / 60, 1)
            remaining = round(
                max(0, MAX_TRADE_DURATION_SECONDS - (time.time() - open_pos["opened_at"])) / 60, 1
            )
            direction_label = "same direction" if open_dir == action else "opposing direction"

            logger.warning(
                f"Signal ignored ({direction_label}): {instrument} already has an open "
                f"{open_dir.upper()} trade (opened {elapsed} min ago via {open_pos['signal']}). "
                f"Auto-expires in {remaining} min."
            )
            send_telegram_message(
                f"\u26a0\ufe0f <b>Signal Ignored \u2014 Trade Already Open</b>\n\n"
                f"<b>Instrument:</b> {params['label']}\n"
                f"<b>Open trade:</b> {open_dir.upper()} @ {open_pos['price']}\n"
                f"<b>Opened via:</b> {open_pos['signal']}\n"
                f"<b>Open for:</b> {elapsed} min\n"
                f"<b>Auto-expires in:</b> {remaining} min\n\n"
                f"<b>Ignored signal:</b> {signal} ({action.upper()})\n"
                f"<i>Letting open trade reach TP or SL naturally.</i>"
            )
            return jsonify({
                "status":             "ignored",
                "reason":             f"Trade already open on {instrument} ({open_dir}, {elapsed} min)",
                "expires_in_minutes": remaining,
            }), 200

        # ── 8. Build TradersPost entry payload ────────────────────────────────
        tp_payload = {
            "ticker":     params["ticker"],
            "action":     action,
            "price":      price,
            "quantity":   params["quantity"],
            "takeProfit": {"amount": params["tp_points"]},
            "stopLoss":   {"amount": params["sl_points"]},
        }
        logger.info(f"Forwarding to TradersPost ({params['label']}): {tp_payload}")

        # ── 9. Forward to TradersPost ─────────────────────────────────────────
        trade_success = False
        tp_error_msg  = ""

        if params["webhook_url"]:
            try:
                tp_resp = requests.post(params["webhook_url"], json=tp_payload, timeout=10)
                logger.info(f"TradersPost response: {tp_resp.status_code} — {tp_resp.text}")
                if tp_resp.ok:
                    trade_success = True
                else:
                    tp_error_msg = f"TradersPost returned {tp_resp.status_code}: {tp_resp.text}"
                    logger.error(tp_error_msg)
            except Exception as e:
                tp_error_msg = f"Exception forwarding to TradersPost: {e}"
                logger.error(tp_error_msg)
        else:
            tp_error_msg = f"TradersPost webhook URL not configured for {instrument}"
            logger.warning(tp_error_msg)

        # ── 10. Record position and notify ────────────────────────────────────
        if trade_success:
            set_open_position(instrument, action, signal, price)

            emoji = "\U0001f7e2" if action == "buy" else "\U0001f534"
            contrarian_tag = "\n<b>⚠️ CONTRARIAN MODE:</b> direction reversed" if CONTRARIAN_MODE else ""
            msg = (
                f"<b>{emoji} Trade Entry Alert</b>{contrarian_tag}\n\n"
                f"<b>Signal:</b> {signal}\n"
                f"<b>Instrument:</b> {params['label']}\n"
                f"<b>Action:</b> {action.upper()}\n"
                f"<b>Quantity:</b> {params['quantity']}\n"
                f"<b>Price:</b> {price}\n"
                f"<b>TP:</b> {params['tp_points']} pts/contract "
                f"(${params['tp_per_ctr']:.2f}/contract, "
                f"${params['tp_per_ctr'] * params['quantity']:.2f} total)\n"
                f"<b>SL:</b> {params['sl_points']} pts/contract "
                f"(${params['sl_per_ctr']:.2f}/contract, "
                f"${params['sl_per_ctr'] * params['quantity']:.2f} total)\n"
                f"<b>Auto-expires:</b> {OPEN_POSITIONS[instrument]['expires_at_str']}"
            )
            send_telegram_message(msg)
            return jsonify({
                "status":  "success",
                "message": f"Signal {signal} processed for {instrument}",
            }), 200

        else:
            error_msg = (
                f"<b>\u26a0\ufe0f TRADE FAILED</b>\n\n"
                f"<b>Signal:</b> {signal}\n"
                f"<b>Instrument:</b> {params['label']}\n"
                f"<b>Error:</b> {tp_error_msg}"
            )
            send_telegram_message(error_msg)
            return jsonify({
                "error":   "Trade forwarding failed",
                "details": tp_error_msg,
            }), 502

    except Exception as e:
        logger.error(f"Unhandled error in /webhook: {e}")
        return jsonify({"error": "Internal server error"}), 500


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
