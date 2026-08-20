"""Deterministic weighted routing with explicit ordered fallback."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    id: str
    provider: str
    model: str
    base_url: str
    weight: int = 0
    fallbacks: tuple[str, ...] = ()
    enabled: bool = True
    circuit_open: bool = False


@dataclass(frozen=True)
class RoutePolicy:
    alias: str
    version: str
    targets: tuple[Target, ...]
    price_table_version: str | None = None

    def __post_init__(self) -> None:
        ids = [target.id for target in self.targets]
        if not self.alias or not self.version or not ids or len(ids) != len(set(ids)):
            raise ValueError("route alias, version, and unique targets are required")
        known = set(ids)
        for target in self.targets:
            if target.weight < 0 or target.id in target.fallbacks or len(target.fallbacks) != len(set(target.fallbacks)):
                raise ValueError("invalid target weight or fallback chain")
            if any(fallback not in known for fallback in target.fallbacks):
                raise ValueError("fallback target is not defined")

    def ordered_targets(
        self,
        request_id: str,
        target_filter: Callable[[Target], bool] | None = None,
    ) -> tuple[Target, ...]:
        allowed = target_filter or (lambda target: True)
        by_id = {target.id: target for target in self.targets}
        primaries = [target for target in self.targets if _eligible(target) and allowed(target) and target.weight > 0]
        total = sum(target.weight for target in primaries)
        if not total:
            return ()
        digest = hashlib.sha256(f"{self.version}:{request_id}".encode()).digest()
        slot = int.from_bytes(digest[:8], "big") % total
        selected = primaries[-1]
        for target in primaries:
            if slot < target.weight:
                selected = target
                break
            slot -= target.weight
        ordered = [selected]
        ordered.extend(
            by_id[target_id]
            for target_id in selected.fallbacks
            if _eligible(by_id[target_id]) and allowed(by_id[target_id])
        )
        return tuple(dict.fromkeys(ordered))


def _eligible(target: Target) -> bool:
    return target.enabled and not target.circuit_open
