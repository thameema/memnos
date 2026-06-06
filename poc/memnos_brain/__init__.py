"""memnos brain-inspired memory engine (B1+).

Layers: raw_turns (sensory) → episodic (hippocampus) → semantic (neocortex).
B1 = schema + write-time encoding (event segmentation, salience, entity graph).
"""
from .store import BrainStore
from .encode import Encoder, extract_entities, salience
from .consolidate import Consolidator
from .retrieve import Retriever, context_block

__all__ = ["BrainStore", "Encoder", "extract_entities", "salience", "Consolidator",
           "Retriever", "context_block"]
