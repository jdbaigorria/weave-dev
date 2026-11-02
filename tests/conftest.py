"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Callable

import loom
import pytest
from loom import LoomSession
from loom.schemas.config.agent import AgentConfig
from loom.schemas.config.app import AppConfig
from loom.schemas.config.models import CustomModelConfig

from weave.adapters.driven.mutable_file_session import MutableFileSessionManager


class FileLoomEngine:
    """A real Loom build (mock model) persisting sessions under ``root``."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.app = AppConfig(
            conversation="none", session={"provider": "file", "config": {"path": root}}
        )

    def build_agent(self, name: str, session: LoomSession, *, model_params=None):
        config = AgentConfig(
            name=name,
            system_prompt={"prompt": "hi", "is_resolved": True},
            model=CustomModelConfig(import_path="loom.testing.MockModel"),
        )
        return loom.build(config, session, app_config=self.app, model_params=model_params)


@pytest.fixture
def file_engine(tmp_path) -> tuple[FileLoomEngine, Callable[[str], MutableFileSessionManager]]:
    """``(engine, store_for)``: the engine and a mutable store over the same directory."""
    engine = FileLoomEngine(str(tmp_path))

    def store_for(session_id: str) -> MutableFileSessionManager:
        return MutableFileSessionManager(session_id=session_id, storage_dir=str(tmp_path))

    return engine, store_for
