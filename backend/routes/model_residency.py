"""Explicit local TTS residency/prewarm controls; no history or audio writes."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import ProfileSample, VoiceProfile, get_db
from ..services.model_residency import model_residency

router = APIRouter(prefix="/models", tags=["models"])


class ResidencyUpdate(BaseModel):
    max_resident_engines: int = Field(ge=1, le=4, strict=True)
    estimated_weight_budget_mb: int = Field(ge=512, le=65536, strict=True)


class PrewarmRequest(BaseModel):
    model_name: str = Field(min_length=1, max_length=100)
    profile_id: str | None = Field(default=None, min_length=1, max_length=100)


@router.get("/residency")
async def residency_status():
    return model_residency.status()


@router.put("/residency")
async def update_residency(request: ResidencyUpdate):
    try:
        return await model_residency.update_policy(request.max_resident_engines, request.estimated_weight_budget_mb)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/prewarm")
async def prewarm_model(request: PrewarmRequest, db: Session = Depends(get_db)):
    from ..backends import TTS_ENGINES, get_model_config
    from ..services import profiles

    configuration = get_model_config(request.model_name)
    if configuration is None or configuration.engine not in TTS_ENGINES:
        raise HTTPException(status_code=400, detail="Select a local TTS model")
    prepare = None
    language = "en"
    if request.profile_id:
        profile = db.query(VoiceProfile).filter_by(id=request.profile_id).first()
        if profile is None:
            raise HTTPException(status_code=404, detail="Voice profile not found")
        try:
            profiles.validate_profile_engine(profile, configuration.engine)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        language = profile.language or "en"
        if language not in configuration.languages:
            raise HTTPException(status_code=400, detail="Selected model does not support this profile language")
        if (profile.voice_type or "cloned") == "cloned":
            samples = db.query(ProfileSample).filter_by(profile_id=request.profile_id).count()
            if samples != 1:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Voice prewarm requires exactly one existing reference sample; use weights-only warm for this profile"
                    ),
                )

        async def prepare():
            return await profiles.create_voice_prompt_for_profile(
                request.profile_id, db, use_cache=True, engine=configuration.engine
            )

    try:
        return await model_residency.prewarm(
            configuration, profile_id=request.profile_id, prepare_prompt=prepare, language=language
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
