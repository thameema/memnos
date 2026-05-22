"""Knowledge graph skills — entity lookup, relation traversal, fact recording."""
import logging

from engram.skills.decorator import skill

logger = logging.getLogger(__name__)


@skill(
    name="graph_query",
    description=(
        "Execute a read-only Cypher query against the Neo4j knowledge graph. "
        "Use this for precise, structured queries when you know the graph schema — "
        "for example, finding all entities of a given type, following specific relation "
        "chains, or aggregating graph statistics. "
        "The $namespace parameter is automatically injected; reference it in your Cypher "
        "to scope results to the correct namespace."
    ),
    parameters={
        "cypher": {
            "type": "string",
            "description": (
                "A read-only Cypher statement (must begin with MATCH, CALL, WITH, or RETURN). "
                "Use $namespace to scope the query to the current namespace."
            ),
        },
        "namespace": {
            "type": "string",
            "description": "Engram namespace to query within (injected as $namespace).",
            "default": "personal:default",
        },
    },
    required=["cypher"],
)
async def graph_query(
    cypher: str,
    namespace: str = "personal:default",
    **kwargs,
) -> dict:
    client = kwargs.get("engram_client")
    if client is None:
        return {"error": "engram_client not provided", "rows": []}
    try:
        rows = await client.query_graph(cypher, namespace)
        return {"rows": rows, "count": len(rows), "namespace": namespace}
    except Exception as exc:
        logger.warning("graph_query failed: %s", exc)
        return {"error": str(exc), "rows": []}


@skill(
    name="get_entity",
    description=(
        "Look up a named entity in the knowledge graph by its exact name. "
        "Returns the entity's type, attributes, and internal ID if found. "
        "Use this to check whether a concept, person, organisation, or other named "
        "thing has been recorded in the graph before trying to traverse its relations."
    ),
    parameters={
        "name": {
            "type": "string",
            "description": "Exact name of the entity to look up (e.g. 'Alice', 'Acme Corp').",
        },
        "namespace": {
            "type": "string",
            "description": "Engram namespace to search within.",
            "default": "personal:default",
        },
    },
    required=["name"],
)
async def get_entity(
    name: str,
    namespace: str = "personal:default",
    **kwargs,
) -> dict:
    client = kwargs.get("engram_client")
    if client is None:
        return {"error": "engram_client not provided", "found": False}
    try:
        entity = await client.get_entity(name, namespace)
        if entity is None:
            return {"found": False, "name": name}
        return {
            "found": True,
            "id": entity.id,
            "name": entity.name,
            "type": entity.entity_type,
            "attributes": entity.attributes,
            "namespace": namespace,
        }
    except Exception as exc:
        logger.warning("get_entity failed: %s", exc)
        return {"error": str(exc), "found": False, "name": name}


@skill(
    name="get_related",
    description=(
        "Retrieve a sub-graph of entities and relations connected to a named entity. "
        "Use this to explore what the knowledge graph knows about a concept — who or what "
        "is connected to it, and how. Depth controls how many relationship hops to follow "
        "(depth=1 returns only direct neighbours; depth=2 includes neighbours-of-neighbours)."
    ),
    parameters={
        "entity_name": {
            "type": "string",
            "description": "Name of the starting entity to traverse from.",
        },
        "namespace": {
            "type": "string",
            "description": "Engram namespace to traverse within.",
            "default": "personal:default",
        },
        "depth": {
            "type": "integer",
            "description": "Maximum number of relationship hops to follow (default 2).",
            "default": 2,
        },
    },
    required=["entity_name"],
)
async def get_related(
    entity_name: str,
    namespace: str = "personal:default",
    depth: int = 2,
    **kwargs,
) -> dict:
    client = kwargs.get("engram_client")
    if client is None:
        return {"error": "engram_client not provided", "entities": [], "relations": []}
    try:
        graph = await client.get_related(entity_name, namespace, depth=depth)
        return {
            "entities": [
                {"id": e.id, "name": e.name, "type": e.entity_type}
                for e in graph.entities
            ],
            "relations": [
                {
                    "source": r.source_entity_id,
                    "target": r.target_entity_id,
                    "type": r.relation_type,
                }
                for r in graph.relations
            ],
            "entity_count": len(graph.entities),
            "relation_count": len(graph.relations),
            "root": entity_name,
            "depth": depth,
            "namespace": namespace,
        }
    except Exception as exc:
        logger.warning("get_related failed: %s", exc)
        return {"error": str(exc), "entities": [], "relations": []}


@skill(
    name="add_fact",
    description=(
        "Record a subject-predicate-object fact in the knowledge graph. "
        "Use this to capture structured, relationship-style information that should be "
        "queryable later — for example: subject='Alice', predicate='works at', object='Acme Corp'. "
        "Facts are more structured than memories and are stored as graph edges, making them "
        "ideal for relationships, properties, and assertions about named entities."
    ),
    parameters={
        "subject": {
            "type": "string",
            "description": "The entity or concept that the fact is about (e.g. 'Alice').",
        },
        "predicate": {
            "type": "string",
            "description": "The relationship or property (e.g. 'works at', 'is a', 'has role').",
        },
        "object": {
            "type": "string",
            "description": "The target entity or value (e.g. 'Acme Corp', 'engineer').",
        },
        "namespace": {
            "type": "string",
            "description": "Engram namespace to store the fact in.",
            "default": "personal:default",
        },
    },
    required=["subject", "predicate", "object"],
)
async def add_fact(
    subject: str,
    predicate: str,
    object: str,
    namespace: str = "personal:default",
    **kwargs,
) -> dict:
    client = kwargs.get("engram_client")
    if client is None:
        return {"error": "engram_client not provided"}
    try:
        fact = await client.add_fact(subject, predicate, object, namespace)
        return {
            "id": fact.id,
            "subject": fact.subject,
            "predicate": fact.predicate,
            "object": fact.object,
            "namespace": namespace,
        }
    except Exception as exc:
        logger.warning("add_fact failed: %s", exc)
        return {"error": str(exc)}
