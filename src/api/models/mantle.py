"""Passthrough client for the bedrock-mantle endpoint.

Mantle is Bedrock's native OpenAI-compatible endpoint. Because requests and
responses are already in OpenAI shape, this module forwards the client's RAW
request bytes and returns Mantle's RAW response — it does NOT round-trip through
the gateway's Pydantic schema (which is modelled for the Converse subset and
would silently drop OpenAI fields it doesn't know about, e.g. seed,
response_format, logit_bias, stream_options).

Routing decision lives in is_mantle_model(): a model served by Mantle is proxied
here; everything else (e.g. Llama, which Mantle does not expose) falls through to
bedrock-runtime/Converse in models/bedrock.py.
"""

import logging

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from api.setting import MANTLE_REGION

logger = logging.getLogger(__name__)

MANTLE_HOST = f"bedrock-mantle.{MANTLE_REGION}.api.aws"
MANTLE_BASE_URL = f"https://{MANTLE_HOST}/v1"

# SigV4 signing service name. This is "bedrock" (NOT "bedrock-mantle") — the
# endpoint is signed under the bedrock service even though the IAM *action*
# prefix for authorization is bedrock-mantle. Do not "fix" this to match the IAM
# prefix: doing so produces SignatureDoesNotMatch errors.
_SIGV4_SERVICE = "bedrock"

# (connect timeout, read timeout). Read is generous for long completions; note
# that a fronting API Gateway/ALB will impose its own (shorter) ceiling.
_TIMEOUT = (10, 900)

# Reused HTTP session for connection pooling (avoids a TLS handshake per call).
_session_http = requests.Session()

# boto3 session sources credentials for SigV4 signing only.
_boto_session = boto3.Session(region_name=MANTLE_REGION)

# Cache of model ids Mantle serves, used for routing. Empty until first load.
mantle_model_set: set[str] = set()
# True once a successful load has happened, so we don't keep retrying a genuinely
# unreachable endpoint on every request (a transient failure still retries while
# the set is empty).
_load_attempted = False


def _signed_headers(method: str, url: str, body: bytes | None) -> dict:
    """Return SigV4-signed headers for a request to the Mantle endpoint."""
    creds = _boto_session.get_credentials()
    if creds is None:
        raise RuntimeError("No AWS credentials available for Mantle SigV4 signing")
    aws_req = AWSRequest(
        method=method,
        url=url,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    SigV4Auth(creds.get_frozen_credentials(), _SIGV4_SERVICE, MANTLE_REGION).add_auth(aws_req)
    return dict(aws_req.headers)


def refresh_mantle_models() -> bool:
    """Fetch the set of model ids Mantle serves and cache it for routing.

    Returns True on success. On failure the previously cached set is LEFT
    UNCHANGED (we never wipe a good set because of a transient blip) and False is
    returned. Blocking; call via run_in_threadpool from async code.
    """
    global mantle_model_set, _load_attempted
    url = f"{MANTLE_BASE_URL}/models"
    try:
        headers = _signed_headers("GET", url, None)
        resp = _session_http.get(url, headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        ids = {m["id"] for m in resp.json().get("data", [])}
        mantle_model_set = ids
        _load_attempted = True
        logger.info("Mantle model list refreshed: %d models", len(ids))
        return True
    except Exception as e:
        logger.warning("Could not refresh Mantle model list: %s", e)
        return False


def ensure_models_loaded() -> None:
    """Lazily load the model set if it has never been successfully loaded.

    This is the safety net for runtimes where the startup hook does not fire
    (e.g. Lambda + Mangum without lifespan support): without it, the set would
    stay empty and every Mantle model would silently misroute to Converse.
    Blocking; call via run_in_threadpool from async code.
    """
    if not _load_attempted:
        refresh_mantle_models()


def is_mantle_model(model_id: str) -> bool:
    """True if the model should be routed to Mantle rather than Converse.

    Pure set membership — does no I/O. Callers must have ensured the set is
    loaded (see ensure_models_loaded)."""
    return model_id in mantle_model_set


def chat_completion(raw_body: bytes) -> requests.Response:
    """Proxy a non-streaming /v1/chat/completions request to Mantle.

    Forwards the raw request body and returns the raw requests.Response so the
    caller can pass through Mantle's status code and body verbatim (including
    error bodies). Blocking; call via run_in_threadpool.
    """
    url = f"{MANTLE_BASE_URL}/chat/completions"
    headers = _signed_headers("POST", url, raw_body)
    return _session_http.post(url, data=raw_body, headers=headers, timeout=_TIMEOUT)


def open_chat_stream(raw_body: bytes) -> requests.Response:
    """Open a streaming /v1/chat/completions request to Mantle.

    Returns the (unconsumed) streaming requests.Response. The caller should
    inspect status_code BEFORE handing the body to a StreamingResponse, so an
    error status can be mapped properly instead of being hidden inside an
    already-started 200 stream. Blocking; call via run_in_threadpool.
    """
    url = f"{MANTLE_BASE_URL}/chat/completions"
    headers = _signed_headers("POST", url, raw_body)
    return _session_http.post(url, data=raw_body, headers=headers, stream=True, timeout=_TIMEOUT)
