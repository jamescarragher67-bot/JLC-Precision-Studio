"""Build stage: placeholder until a builder is configured.

Until config.BUILDER_MODEL is set, this stage refuses to run. It marks the job
failed with reason "builder not configured" and makes no API call.
"""

from __future__ import annotations

from app import config
from app.stages import StageContext, StageFailed, StageOutput


def run(ctx: StageContext) -> StageOutput:
    if config.BUILDER_MODEL is None:
        raise StageFailed("builder not configured")

    # TODO(astra): once BUILDER_MODEL is set, call the builder through
    # api_client.call_model(role="build", ...) and write the model into
    # the job's model/ folder. Until that is written, refuse rather than guess.
    raise StageFailed("builder configured but the build step is not implemented yet")
