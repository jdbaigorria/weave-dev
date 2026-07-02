"""Smoke tests: the skeleton imports and the Loom dependency is wired."""


def test_weave_imports():
    import weave

    assert weave.__version__


def test_layers_import():
    # Every hexagonal layer is importable.
    import importlib

    for mod in (
        "weave.domain",
        "weave.application",
        "weave.ports",
        "weave.adapters",
        "weave.adapters.driven",
        "weave.adapters.driving",
        "weave.composition",
    ):
        assert importlib.import_module(mod) is not None

    from weave.ports.agent_engine import AgentEngine

    assert AgentEngine is not None


def test_loom_dependency_available():
    # Weave depends on Loom and reaches it through the public API.
    import loom

    assert hasattr(loom, "build")
