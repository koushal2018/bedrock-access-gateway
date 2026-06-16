import logging
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from api.auth import api_key_auth
from api.models import mantle
from api.models.bedrock import BedrockModel
from api.schema import ChatRequest, ChatResponse, ChatStreamResponse, Error
from api.setting import DEFAULT_MODEL, ENABLE_MANTLE

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/chat",
    dependencies=[Depends(api_key_auth)],
    # responses={404: {"description": "Not found"}},
)


@router.post(
    "/completions", response_model=ChatResponse | ChatStreamResponse | Error, response_model_exclude_unset=True
)
async def chat_completions(
    request: Request,
    chat_request: Annotated[
        ChatRequest,
        Body(
            examples=[
                {
                    "model": "anthropic.claude-3-sonnet-20240229-v1:0",
                    "messages": [
                        {"role": "system", "content": "You are a helpful assistant."},
                        {"role": "user", "content": "Hello!"},
                    ],
                }
            ],
        ),
    ],
):
    # Route Mantle-served models to the bedrock-mantle endpoint, forwarding the
    # client's RAW request and Mantle's RAW response (Mantle is already
    # OpenAI-shaped — no Converse translation, and no Pydantic round-trip that
    # would drop OpenAI fields the gateway schema doesn't model). Everything else
    # (e.g. Llama, which Mantle does not expose) falls through to Converse below.
    # Note: the gpt-* -> DEFAULT_MODEL rewrite is intentionally NOT applied here,
    # so genuine OpenAI models hosted on Mantle reach Mantle unaltered.
    if ENABLE_MANTLE:
        await run_in_threadpool(mantle.ensure_models_loaded)
        if mantle.is_mantle_model(chat_request.model):
            return await _proxy_to_mantle(request, chat_request.stream)

    if chat_request.model.lower().startswith("gpt-"):
        chat_request.model = DEFAULT_MODEL

    # Exception will be raised if model not supported.
    model = BedrockModel()
    model.validate(chat_request)
    if chat_request.stream:
        return StreamingResponse(content=model.chat_stream(chat_request), media_type="text/event-stream")
    return await model.chat(chat_request)


async def _proxy_to_mantle(request: Request, stream: bool | None):
    """Forward the raw request to Mantle and return its raw response.

    Status codes and bodies (including Mantle's OpenAI-shaped error bodies) are
    passed through verbatim. For streaming, the upstream status is checked before
    a 200 stream is started, so an error isn't hidden inside an already-committed
    200 response.
    """
    raw_body = await request.body()
    try:
        if stream:
            upstream = await run_in_threadpool(mantle.open_chat_stream, raw_body)
            if upstream.status_code >= 400:
                # Surface the error instead of starting an empty 200 stream.
                content = await run_in_threadpool(lambda: upstream.content)
                upstream.close()
                return Response(
                    content=content,
                    status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type", "application/json"),
                )

            def iter_upstream():
                try:
                    for chunk in upstream.iter_content(chunk_size=None):
                        if chunk:
                            yield chunk
                finally:
                    upstream.close()

            return StreamingResponse(iter_upstream(), media_type="text/event-stream")

        upstream = await run_in_threadpool(mantle.chat_completion, raw_body)
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )
    except Exception as e:
        # Connectivity / signing / timeout failures reaching Mantle.
        logger.error("Mantle proxy request failed: %s", e)
        return JSONResponse(
            status_code=502,
            content={"error": {"message": f"Failed to reach Mantle endpoint: {e}", "type": "upstream_error"}},
        )
