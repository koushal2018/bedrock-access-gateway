from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from api.auth import api_key_auth
from api.models import mantle
from api.models.bedrock import BedrockModel
from api.schema import Model, Models
from api.setting import ENABLE_MANTLE

router = APIRouter(
    prefix="/models",
    dependencies=[Depends(api_key_auth)],
    # responses={404: {"description": "Not found"}},
)

chat_model = BedrockModel()


def _all_model_ids() -> list[str]:
    """Bedrock/Converse models plus Mantle-served models (when enabled)."""
    ids = chat_model.list_models()
    if ENABLE_MANTLE:
        # Ensure the set is loaded at least once, then read the cached set.
        # We do NOT force a refresh here: a transient Mantle failure on this read
        # path must not be able to wipe the routing set used by /chat/completions.
        mantle.ensure_models_loaded()
        seen = set(ids)
        # Preserve order, drop duplicates (a model may be on both endpoints).
        ids = ids + [m for m in sorted(mantle.mantle_model_set) if m not in seen]
    return ids


async def validate_model_id(model_id: str):
    if model_id not in _all_model_ids():
        raise HTTPException(status_code=500, detail="Unsupported Model Id")


@router.get("", response_model=Models)
async def list_models():
    model_list = [Model(id=model_id) for model_id in _all_model_ids()]
    return Models(data=model_list)


@router.get(
    "/{model_id}",
    response_model=Model,
)
async def get_model(
    model_id: Annotated[
        str,
        Path(description="Model ID", example="anthropic.claude-3-sonnet-20240229-v1:0"),
    ],
):
    await validate_model_id(model_id)
    return Model(id=model_id)
