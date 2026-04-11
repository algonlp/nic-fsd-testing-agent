import os
import re
import logging
from pathlib import Path
from collections import defaultdict, deque
from threading import Lock
from time import time
from urllib.parse import urlparse
from flask import Flask, jsonify, request, render_template
from dotenv import load_dotenv
import requests
from werkzeug.exceptions import RequestEntityTooLarge

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)
app.logger.setLevel(logging.INFO)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_CONTENT_LENGTH_BYTES", "4096"))

E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
PHONE_ID_RE = re.compile(r"^phnum_[a-z0-9]+$", re.IGNORECASE)
ELEVENLABS_ENDPOINT = os.getenv(
    "ELEVENLABS_ENDPOINT",
    "https://api.elevenlabs.io/v1/convai/twilio/outbound-call",
)
ALLOWED_ORIGINS = {
    origin.strip().rstrip("/")
    for origin in os.getenv("ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
}
RATE_LIMIT_WINDOW_SEC = max(1, int(os.getenv("CALL_RATE_LIMIT_WINDOW_SEC", "300")))
RATE_LIMIT_MAX_REQUESTS = max(1, int(os.getenv("CALL_RATE_LIMIT_MAX_REQUESTS", "5")))
UPSTREAM_CONNECT_TIMEOUT_SEC = max(1, int(os.getenv("UPSTREAM_CONNECT_TIMEOUT_SEC", "10")))
UPSTREAM_READ_TIMEOUT_SEC = max(1, int(os.getenv("UPSTREAM_READ_TIMEOUT_SEC", "20")))
CALL_RATE_LIMITS = defaultdict(deque)
CALL_RATE_LIMIT_LOCK = Lock()


def is_e164(value: str) -> bool:
    return bool(E164_RE.match(value or ""))


def normalize_number(raw: str, country_code: str) -> str:
    raw = (raw or "").strip()
    country_code = (country_code or "").strip()
    if raw.startswith("+"):
        return "+" + re.sub(r"\D", "", raw)

    digits_only = re.sub(r"\D", "", raw)
    cc_digits = re.sub(r"\D", "", country_code)
    if not digits_only:
        return ""
    if digits_only.startswith(cc_digits) and cc_digits:
        return f"+{digits_only}"
    if digits_only.startswith("0") and cc_digits:
        return f"+{cc_digits}{digits_only.lstrip('0')}"
    if cc_digits:
        return f"+{cc_digits}{digits_only}"
    return f"+{digits_only}"


def extract_error_message(data, fallback: str) -> str:
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip()

        detail = data.get("detail")
        if isinstance(detail, dict):
            message = detail.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        elif isinstance(detail, list):
            messages = []
            for item in detail:
                if isinstance(item, dict):
                    message = item.get("msg") or item.get("message")
                    if isinstance(message, str) and message.strip():
                        messages.append(message.strip())
                elif isinstance(item, str) and item.strip():
                    messages.append(item.strip())
            if messages:
                return "; ".join(messages)

        eleven_response = data.get("eleven_response")
        if isinstance(eleven_response, dict):
            message = eleven_response.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()

        message = data.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()

    if isinstance(data, str) and data.strip():
        return data.strip()

    return fallback


def mask_value(value: str, prefix: int = 6, suffix: int = 4) -> str:
    value = (value or "").strip()
    if len(value) <= prefix + suffix:
        return value
    return f"{value[:prefix]}...{value[-suffix:]}"


def get_request_origin() -> str:
    origin = (request.headers.get("Origin") or "").strip()
    if origin:
        return origin.rstrip("/")

    referer = (request.headers.get("Referer") or "").strip()
    if not referer:
        return ""

    parsed = urlparse(referer)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    return ""


def get_client_ip() -> str:
    forwarded_for = (request.headers.get("X-Forwarded-For") or "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip() or "unknown"
    return (request.headers.get("X-Real-IP") or request.remote_addr or "unknown").strip() or "unknown"


def check_rate_limit(client_ip: str) -> int:
    now = time()
    threshold = now - RATE_LIMIT_WINDOW_SEC

    with CALL_RATE_LIMIT_LOCK:
        bucket = CALL_RATE_LIMITS[client_ip]
        while bucket and bucket[0] <= threshold:
            bucket.popleft()

        if len(bucket) >= RATE_LIMIT_MAX_REQUESTS:
            retry_after = max(1, int(bucket[0] + RATE_LIMIT_WINDOW_SEC - now))
            return retry_after

        bucket.append(now)
        return 0


def summarize_for_log(data):
    if isinstance(data, dict):
        summary = {}
        for key in ("success", "message", "error", "status", "call_id", "callSid"):
            if key in data:
                summary[key] = data.get(key)

        eleven_response = data.get("eleven_response")
        if isinstance(eleven_response, dict):
            nested = {}
            for key in ("success", "message", "error", "status", "call_id", "callSid"):
                if key in eleven_response:
                    nested[key] = eleven_response.get(key)
            if nested:
                summary["eleven_response"] = nested

        return summary or {"keys": sorted(data.keys())[:10]}

    if isinstance(data, str):
        return data[:200]

    return {"type": type(data).__name__}


@app.get("/")
def index():
    return render_template("widget.html")


@app.errorhandler(RequestEntityTooLarge)
def handle_request_too_large(_exc):
    return jsonify({"error": "Request body too large."}), 413


@app.post("/api/call")
def create_call():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        app.logger.warning("Rejected /api/call request with non-object JSON body")
        return jsonify({"error": "Invalid request body. Expected a JSON object."}), 400

    raw_number = payload.get("toNumber", "")
    country_code = payload.get("countryCode", "")
    agent_id = (os.getenv("ELEVENLABS_AGENT_ID") or "").strip()
    phone_id = (os.getenv("ELEVENLABS_PHONE_ID") or "").strip()
    elevenlabs_api_key = (os.getenv("ELEVENLABS_API_KEY") or "").strip()
    endpoint = ELEVENLABS_ENDPOINT.strip()
    request_origin = get_request_origin()
    client_ip = get_client_ip()

    if not isinstance(raw_number, str) or not isinstance(country_code, str):
        app.logger.warning("Rejected /api/call due to invalid phone payload types")
        return jsonify({"error": "Invalid phone number. Please enter a valid number."}), 400
    if os.getenv("VERCEL_ENV") and not ALLOWED_ORIGINS:
        app.logger.error("Rejected /api/call because ALLOWED_ORIGINS is not configured for Vercel")
        return jsonify({"error": "Server origin allowlist is not configured."}), 503
    if ALLOWED_ORIGINS and request_origin not in ALLOWED_ORIGINS:
        app.logger.warning(
            "Rejected /api/call due to disallowed origin origin=%s ip=%s",
            request_origin or "<missing>",
            client_ip,
        )
        return jsonify({"error": "Request origin is not allowed."}), 403

    retry_after = check_rate_limit(client_ip)
    if retry_after:
        app.logger.warning("Rejected /api/call due to rate limit ip=%s retry_after=%s", client_ip, retry_after)
        response = jsonify({"error": "Too many call requests. Please try again shortly."})
        response.status_code = 429
        response.headers["Retry-After"] = str(retry_after)
        return response

    to_number = normalize_number(raw_number, country_code)
    if not to_number or not is_e164(to_number):
        app.logger.warning("Rejected /api/call due to invalid E.164 number: %s", to_number or "<empty>")
        return jsonify({"error": "Invalid phone number. Please enter a valid number."}), 400
    if not elevenlabs_api_key:
        app.logger.error("Rejected /api/call because ELEVENLABS_API_KEY is missing")
        return jsonify({"error": "Missing ELEVENLABS_API_KEY."}), 400
    if not agent_id:
        app.logger.error("Rejected /api/call because ELEVENLABS_AGENT_ID is missing")
        return jsonify({"error": "Missing ELEVENLABS_AGENT_ID / agentId."}), 400
    if not phone_id:
        app.logger.error("Rejected /api/call because ELEVENLABS_PHONE_ID is missing")
        return jsonify({"error": "Missing ELEVENLABS_PHONE_ID / phoneId."}), 400
    if not endpoint:
        app.logger.error("Rejected /api/call because ELEVENLABS_ENDPOINT is missing")
        return jsonify({"error": "Missing ELEVENLABS_ENDPOINT."}), 400
    if not agent_id.startswith("agent_"):
        app.logger.warning("Rejected /api/call due to invalid agent_id format: %s", agent_id)
        return jsonify({"error": "ELEVENLABS_AGENT_ID must be a valid ElevenLabs agent ID."}), 400
    if phone_id.startswith("agent_") or phone_id == agent_id or not PHONE_ID_RE.match(phone_id):
        app.logger.warning("Rejected /api/call due to invalid phone_id format: %s", phone_id)
        return jsonify({"error": "ELEVENLABS_PHONE_ID must be a phone number ID, not an agent ID."}), 400
    if not endpoint.startswith("https://"):
        app.logger.warning("Rejected /api/call due to non-https endpoint: %s", endpoint)
        return jsonify({"error": "ELEVENLABS_ENDPOINT must be an https URL."}), 400

    try:
        headers = {"xi-api-key": elevenlabs_api_key, "Content-Type": "application/json"}
        request_body = {
            "agent_id": agent_id,
            "agent_phone_number_id": phone_id,
            "to_number": to_number,
        }
        app.logger.info(
            "Forwarding outbound call request to ElevenLabs endpoint=%s agent_id=%s phone_id=%s to_number=%s",
            endpoint,
            mask_value(agent_id),
            mask_value(phone_id),
            mask_value(to_number),
        )
        resp = requests.post(
            endpoint,
            json=request_body,
            headers=headers,
            timeout=(UPSTREAM_CONNECT_TIMEOUT_SEC, UPSTREAM_READ_TIMEOUT_SEC),
        )
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}

        app.logger.info(
            "ElevenLabs response status=%s body=%s",
            resp.status_code,
            summarize_for_log(data),
        )

        if not resp.ok:
            return jsonify({"error": extract_error_message(data, "Call failed")}), resp.status_code

        eleven_response = data.get("eleven_response") if isinstance(data, dict) else None
        if isinstance(eleven_response, dict) and eleven_response.get("success") is False:
            return jsonify({"error": extract_error_message(eleven_response, "Call failed")}), 502

        if isinstance(data, dict) and data.get("success") is False:
            return jsonify({"error": extract_error_message(data, "Call failed")}), 502

        return jsonify(data)
    except requests.exceptions.ConnectTimeout:
        app.logger.exception("Timeout connecting to ElevenLabs endpoint")
        return jsonify({"error": "Unable to reach ElevenLabs endpoint."}), 504
    except requests.exceptions.ReadTimeout:
        app.logger.exception("Read timeout from ElevenLabs endpoint")
        return jsonify({"error": "ElevenLabs endpoint timed out while processing the call."}), 504
    except requests.exceptions.RequestException:
        app.logger.exception("Requests error while calling ElevenLabs")
        return jsonify({"error": "Unable to complete the call request right now."}), 502
    except Exception:
        app.logger.exception("Unexpected proxy error while creating call")
        return jsonify({"error": "Unexpected proxy error."}), 502


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "").lower() in {"1", "true", "yes", "on"}
    app.run(host="0.0.0.0", port=port, debug=debug)

