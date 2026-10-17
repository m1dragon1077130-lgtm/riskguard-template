"""
RiskGuard — ربات مدیریت ریسک
پارامترها در هر چرخه از config.json خونده می‌شن
"""

import os
import sys
import json
import time
import hmac
import hashlib
import urllib.request
import urllib.error


# ============================================================
# تنظیمات ثابت
# ============================================================
MAX_RUN_TIME_SECONDS = 5 * 3600 + 50 * 60
CHECK_INTERVAL_SECONDS = 20

LOCK_STOP_LOSS = True

BASE_URL = "https://apiv2.thetruetrade.io"
FUTURES_PREFIX = "/futures"

STATE_FILE = "state.json"


# ============================================================
# توابع کمکی
# ============================================================
def sign(secret, timestamp, method, uri):
    payload = f"{timestamp}{method}{uri}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def request(method, uri, body=None, api_key=None, api_secret=None):
    timestamp = str(int(time.time() * 1000))
    signature = sign(api_secret, timestamp, method.upper(), uri)

    url = BASE_URL + uri
    headers = {
        "X-API-Key": api_key,
        "X-Timestamp": timestamp,
        "X-Signature": signature,
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }

    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        print(f"خطای HTTP {e.code} در {method} {uri}: {err_body}")
        return None
    except Exception as e:
        print(f"خطا در {method} {uri}: {e}")
        return None


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"خطا در خواندن فایل state: {e}")
            return {}
    return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"خطا در ذخیره فایل state: {e}")


def load_current_config():
    """
    تنظیمات رو از config.json می‌خونه.
    اگه فایل در حال آپدیت بود (خطا داد)، مقادیر پیش‌فرض برمی‌گردونه.
    """
    defaults = {
        "max_loss_usd": 1.0,
        "reward_ratio": 5.0,
        "trigger_ratio": 2.0,
        "lock_profit_ratio": 1.0,
        "max_positions": 1,
        "is_active": True,
        "tt_api_key": None,
        "tt_api_secret": None,
    }
    
    try:
        with open("config.json", "r") as f:
            cfg = json.load(f)
        # مقادیر فایل رو با defaults ادغام می‌کنیم (اگه کلیدی نبود، پیش‌فرض بمونه)
        for k, v in defaults.items():
            if k not in cfg:
                cfg[k] = v
        return cfg
    except Exception as e:
        print(f"⚠️ خطا در خواندن config.json: {e}")
        return defaults


# ============================================================
# چرخه‌ی اصلی
# ============================================================
def run_riskguard_cycle(state):
    # 🌟 هر بار تنظیمات رو از اول می‌خونیم
    CFG = load_current_config()
    
    API_KEY = CFG.get("tt_api_key")
    API_SECRET = CFG.get("tt_api_secret")
    
    if not API_KEY or not API_SECRET:
        print("❌ خطا: tt_api_key یا tt_api_secret توی config.json نیست")
        return
    
    MAX_LOSS_USD = CFG.get("max_loss_usd", 1.0)
    REWARD_RATIO = CFG.get("reward_ratio", 5.0)
    TRIGGER_RATIO = CFG.get("trigger_ratio", 2.0)
    LOCK_PROFIT_RATIO = CFG.get("lock_profit_ratio", 1.0)
    MAX_POSITIONS = CFG.get("max_positions", 1)
    IS_ACTIVE = CFG.get("is_active", True)
    
    ONLY_ONE_TRADE = MAX_POSITIONS == 1

    if not IS_ACTIVE:
        print("⏸ ربات غیرفعال است.")
        sys.exit(0)

    response = request("GET", f"{FUTURES_PREFIX}/positions", api_key=API_KEY, api_secret=API_SECRET)
    if response is None:
        print("عدم دریافت اطلاعات پوزیشن‌ها.")
        return

    positions = response.get("items", []) if isinstance(response, dict) else response
    open_positions = [
        p for p in positions
        if isinstance(p, dict) and p.get("status") == "OPENED" and p.get("isActive")
    ]

    if not open_positions:
        return

    # قانون تک معامله / حداکثر معاملات
    if ONLY_ONE_TRADE and len(open_positions) > 1:
        open_positions.sort(key=lambda p: (p.get("createdAt", ""), p.get("id")))
        oldest_position = open_positions[0]
        oldest_id = oldest_position["id"]
        print(f"قدیمی‌ترین پوزیشن نگه داشته شد: ID {oldest_id}")

        for p in open_positions[1:]:
            pid = p["id"]
            print(f"بستن پوزیشن جدیدتر {pid}")
            request("POST", f"{FUTURES_PREFIX}/positions/{pid}/close", {"orderType": "MARKET"}, api_key=API_KEY, api_secret=API_SECRET)
        open_positions = [oldest_position]

    for p in open_positions:
        pid = str(p["id"])
        symbol = p["symbol"]
        side = str(p["side"]).upper()
        entry_price = float(p["entryPrice"])
        size = float(p.get("size", 0))
        current_sl = p.get("stopLoss")
        current_tp = p.get("takeProfit")
        unrealized_pnl = float(p.get("unrealizedPnL", 0) or p.get("pnl", 0) or 0)

        if size <= 0:
            continue

        sl_distance = MAX_LOSS_USD / size
        tp_distance = (MAX_LOSS_USD * REWARD_RATIO) / size
        lock_profit_distance = (MAX_LOSS_USD * LOCK_PROFIT_RATIO) / size

        if side == "LONG":
            target_sl = round(entry_price - sl_distance, 4)
            target_tp = round(entry_price + tp_distance, 4)
            profit_sl_price = round(entry_price + lock_profit_distance, 4)
        else:
            target_sl = round(entry_price + sl_distance, 4)
            target_tp = round(entry_price - tp_distance, 4)
            profit_sl_price = round(entry_price - lock_profit_distance, 4)

        key = pid
        needs_update = False
        payload = {
            "stopLossOrderType": "STOP_MARKET",
            "takeProfitOrderType": "TAKE_PROFIT_MARKET",
        }

        if key not in state:
            state[key] = {
                "symbol": symbol,
                "side": side,
                "entry": entry_price,
                "initial_sl": target_sl,
                "initial_tp": target_tp,
                "sl": target_sl,
                "tp": target_tp,
                "is_profit_locked": False,
            }

        # قفل سود
        trigger_threshold = MAX_LOSS_USD * TRIGGER_RATIO
        if unrealized_pnl >= trigger_threshold and not state[key].get("is_profit_locked", False):
            print(f"🎯 قفل سود {symbol} روی {profit_sl_price}")
            state[key]["sl"] = profit_sl_price
            state[key]["is_profit_locked"] = True
            payload["stopLoss"] = str(profit_sl_price)
            needs_update = True

        # کنترل حد ضرر
        if not needs_update:
            if current_sl is None or str(current_sl).strip() == "" or float(current_sl or 0) == 0:
                print(f"⚠️ SL {symbol} حذف شده بود! تنظیم مجدد")
                payload["stopLoss"] = str(state[key]["sl"])
                needs_update = True
            elif LOCK_STOP_LOSS:
                current_sl_f = float(current_sl)
                saved_sl_f = float(state[key]["sl"])

                risk_increased = False
                if side == "LONG" and current_sl_f < saved_sl_f - 1e-6:
                    risk_increased = True
                elif side == "SHORT" and current_sl_f > saved_sl_f + 1e-6:
                    risk_increased = True

                if risk_increased:
                    print(f"⚠️ افزایش ریسک {symbol}! بازگرداندن SL")
                    payload["stopLoss"] = str(saved_sl_f)
                    needs_update = True
                elif abs(current_sl_f - saved_sl_f) > 1e-6:
                    print(f"SL {symbol} جابجا شد: {saved_sl_f} -> {current_sl_f}")
                    state[key]["sl"] = current_sl_f

        # کنترل حد سود
        target_tp_f = float(state[key]["tp"])
        if current_tp is None or str(current_tp).strip() == "" or float(current_tp or 0) == 0:
            print(f"⚠️ TP {symbol} حذف شده بود! تنظیم مجدد")
            payload["takeProfit"] = str(target_tp_f)
            needs_update = True
        else:
            current_tp_f = float(current_tp)
            too_far = (
                (current_tp_f > target_tp_f + 1e-6) if side == "LONG"
                else (current_tp_f < target_tp_f - 1e-6)
            )
            if too_far:
                print(f"⚠️ TP {symbol} دورتر شده بود! بازگرداندن")
                payload["takeProfit"] = str(target_tp_f)
                needs_update = True

        # اعمال تغییرات
        if needs_update:
            if "stopLoss" not in payload:
                payload["stopLoss"] = str(current_sl) if current_sl else str(state[key]["sl"])
            if "takeProfit" not in payload:
                payload["takeProfit"] = str(current_tp) if current_tp else str(state[key]["tp"])

            result = request("PATCH", f"{FUTURES_PREFIX}/positions/{pid}/tpsl", payload, api_key=API_KEY, api_secret=API_SECRET)
            if result is not None:
                print(f"✅ TP/SL {symbol} آپدیت شد.")
            else:
                print(f"❌ خطا در آپدیت {symbol}")

    open_ids = {str(p["id"]) for p in open_positions}
    for k in list(state.keys()):
        if k not in open_ids:
            del state[k]

    save_state(state)


def main():
    start_time = time.time()
    print("🚀 RiskGuard شروع شد...")

    while True:
        elapsed = time.time() - start_time
        if elapsed >= MAX_RUN_TIME_SECONDS:
            print("⏱ زمان چرخه تموم شد.")
            break

        try:
            state = load_state()
            run_riskguard_cycle(state)
        except Exception as e:
            print(f"خطا: {e}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
