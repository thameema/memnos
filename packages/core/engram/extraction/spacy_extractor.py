"""
engram.extraction.spacy_extractor — Named entity extraction using spaCy.

Extracts entities from memory content to build MENTIONS edges in the
knowledge graph. No LLM call required — runs fully local.

Entity types mapped from spaCy labels:
  PERSON      → PERSON
  ORG         → ORG
  GPE, LOC    → LOCATION
  PRODUCT     → TECH
  WORK_OF_ART → CONCEPT
  EVENT       → EVENT
  LAW         → COMPLIANCE
  NORP        → ORG (nationality, religious, political groups)
  FAC         → LOCATION (buildings, airports)
  Everything else → CONCEPT
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Additional tech-specific patterns not caught by general NER
_TECH_PATTERNS = re.compile(
    r'\b(FHIR|HL7|REST|GraphQL|gRPC|JWT|OAuth|SAML|OIDC|'
    r'Docker|Kubernetes|K8s|Terraform|Helm|'
    r'PostgreSQL|MySQL|Redis|MongoDB|Elasticsearch|'
    r'Python|Java|TypeScript|JavaScript|Rust|Go|'
    r'React|Angular|Vue|FastAPI|Spring Boot|Django|'
    r'AWS|Azure|GCP|S3|EC2|Lambda|'
    r'Git|GitLab|GitHub|CI/CD|DevOps|'
    r'HIPAA|CMS|FHIR R4|HL7 v2|ICD-10|CPT|SNOMED|'
    r'LLM|RAG|MCP|Claude|GPT|Llama|ArcadeDB|Qdrant|Neo4j)\b',
    re.IGNORECASE
)

_SPACY_TO_ENGRAM: dict[str, str] = {
    "PERSON": "PERSON",
    "ORG": "ORG",
    "GPE": "LOCATION",
    "LOC": "LOCATION",
    "FAC": "LOCATION",
    "PRODUCT": "TECH",
    "WORK_OF_ART": "CONCEPT",
    "EVENT": "EVENT",
    "LAW": "COMPLIANCE",
    "NORP": "ORG",
    "LANGUAGE": "TECH",
}


@dataclass
class ExtractedEntity:
    name: str
    entity_type: str


class SpacyExtractor:
    """
    Extract named entities from text using spaCy.

    Falls back to regex-only extraction if spaCy model is not installed,
    so the system remains functional without the full NLP model.
    """

    def __init__(self, model: str = "en_core_web_sm") -> None:
        self._model_name = model
        self._nlp = None
        self._spacy_available = False

    def _load_model(self) -> None:
        """Lazy-load spaCy model. Logs a warning if unavailable."""
        if self._nlp is not None:
            return
        try:
            import spacy  # type: ignore
            try:
                self._nlp = spacy.load(self._model_name)
                self._spacy_available = True
                logger.info("spaCy model %r loaded for entity extraction", self._model_name)
            except OSError:
                logger.warning(
                    "spaCy model %r not found — using regex fallback. "
                    "Install with: python -m spacy download %s",
                    self._model_name,
                    self._model_name,
                )
                import spacy
                self._nlp = spacy.blank("en")
        except ImportError:
            logger.warning(
                "spacy not installed — entity extraction disabled (regex-only). "
                "Install with: pip install spacy && python -m spacy download en_core_web_sm"
            )
            self._spacy_available = False

    def extract_sync(self, text: str) -> list[ExtractedEntity]:
        """Extract entities synchronously."""
        self._load_model()
        entities: dict[str, ExtractedEntity] = {}

        # spaCy NER
        if self._spacy_available and self._nlp is not None:
            try:
                doc = self._nlp(text[:10000])  # cap to 10k chars for performance
                for ent in doc.ents:
                    name_norm = ent.text.strip().lower()
                    if len(name_norm) < 2:
                        continue
                    etype = _SPACY_TO_ENGRAM.get(ent.label_, "CONCEPT")
                    entities[name_norm] = ExtractedEntity(name=name_norm, entity_type=etype)
            except Exception as exc:
                logger.debug("spaCy extraction error (non-fatal): %s", exc)

        # Tech-specific regex patterns (always runs)
        for match in _TECH_PATTERNS.finditer(text):
            name_norm = match.group(0).lower()
            if name_norm not in entities:
                entities[name_norm] = ExtractedEntity(name=name_norm, entity_type="TECH")

        return list(entities.values())

    async def extract(self, text: str) -> list[ExtractedEntity]:
        """Extract entities asynchronously (runs sync extraction in thread pool)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.extract_sync, text)


# Module-level singleton
_extractor: SpacyExtractor | None = None


def get_extractor(model: str = "en_core_web_sm") -> SpacyExtractor:
    """Return the module-level SpacyExtractor singleton."""
    global _extractor
    if _extractor is None:
        _extractor = SpacyExtractor(model=model)
    return _extractor
