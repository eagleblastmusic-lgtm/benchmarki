"""Minimal build-only Browser consumer adapter for the N4 publication boundary.

The adapter has no lifecycle or result writer.  It delegates durable cursor,
presentation-witness and Resume operations to :class:`PublicationStore` and
keeps no semantic cache.  A real Browser integration can use this narrow
boundary later without moving Task/WorkItem/Publication authority into the UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from bdb_vnext.n4_publication import ConsumerBinding, PublicationRecord, PublicationStore, ResumeCapsule


@dataclass(frozen=True)
class BrowserPublicationClient:
    store: PublicationStore
    consumer_id: str
    conversation_id: str
    profile_id: str | None = None
    generation: str | None = None

    def receive_next(self) -> PublicationRecord | None:
        return self.store.receive_next(consumer_id=self.consumer_id, generation=self.generation)

    def receive_from_cursor(self, cursor_sequence: int) -> PublicationRecord | None:
        return self.store.receive_from_cursor(consumer_id=self.consumer_id, cursor_sequence=cursor_sequence, generation=self.generation)

    def acknowledge(self, publication_id: str, *, fault: str | None = None) -> ConsumerBinding:
        return self.store.acknowledge(consumer_id=self.consumer_id, publication_id=publication_id, generation=self.generation, fault=fault)

    def observe_dom(self, publication: PublicationRecord, *, marker: str, composer_preserved: bool = True, witness: Mapping[str, Any] | None = None) -> ConsumerBinding:
        return self.store.observe_presentation(
            publication_id=publication.publication_id,
            consumer_id=self.consumer_id,
            conversation_id=self.conversation_id,
            profile_id=self.profile_id,
            marker=marker,
            result_digest=publication.result_digest,
            generation=self.generation,
            composer_preserved=composer_preserved,
            witness=witness,
        )

    def mark_unknown(self, publication_id: str, *, reason: str) -> ConsumerBinding:
        return self.store.mark_unknown(publication_id=publication_id, consumer_id=self.consumer_id, reason=reason, generation=self.generation)

    def resume_payload(self, capsule: ResumeCapsule) -> dict[str, Any]:
        return self.store.resume_payload(capsule.capsule_id)


__all__ = ["BrowserPublicationClient"]
