"""Handler registration aggregator for the pypsa-earth domain."""

__all__ = ["register_all_handlers", "register_all_registry_handlers"]


def register_all_handlers(poller) -> None:
    from .osm_stage_handlers import register_pypsa_earth_handlers as reg

    reg(poller)


def register_all_registry_handlers(runner) -> None:
    from .osm_stage_handlers import register_handlers as reg

    reg(runner)
