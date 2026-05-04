"""
Trust Graph module.

Current implementation uses SQLAlchemy (SQLite/PostgreSQL).

Neo4j upgrade path:
  When the user count grows to the point relational queries become a bottleneck,
  replace the SQLAlchemy calls below with the Neo4j Python driver and Cypher queries.

  Equivalent Cypher for the draw ceiling query:
    MATCH (start:User {id: $user_id})-[:RECRUITED_BY*1..3]->(trusted:User)
    RETURN trusted.id, trusted.trustScore

  Install: pip install neo4j
  Connect:
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
"""
from loguru import logger
from models import db, User, SystemState


class TrustGraphError(Exception):
    pass


def compute_draw_ceiling(user_id: int) -> float:
    """
    Compute the solidarity draw ceiling for a user based on:
      - Their own trust score (0–1, scaled to max $100)
      - Weighted trust scores of depth-1 recruits
      - Communal pool health factor

    Returns the ceiling amount in dollars (capped at $500).
    """
    try:
        user = User.query.get(user_id)
        if not user:
            raise TrustGraphError(f"User with id={user_id} not found")

        own_contribution = user.trust_score * 100.0

        recruits = user.recruits
        recruit_sum = sum(rec.trust_score * 20.0 * 0.8 for rec in recruits)

        raw_ceiling = own_contribution + recruit_sum

        state = SystemState.query.first()
        pool = state.communal_pool_balance if state else 5000.0

        if pool > 2000:
            health_factor = 1.2
        elif pool < 500:
            health_factor = 0.7
        else:
            health_factor = 1.0

        ceiling = round(raw_ceiling * health_factor, 2)
        ceiling = min(ceiling, 500.0)

        logger.info(
            "Computed draw ceiling for user_id={}: own={:.2f}, recruits={:.2f}, "
            "pool_health={}, ceiling={:.2f}",
            user_id, own_contribution, recruit_sum, health_factor, ceiling
        )
        return ceiling

    except TrustGraphError:
        raise
    except Exception as exc:
        logger.error("Unexpected error in compute_draw_ceiling for user_id={}: {}", user_id, exc)
        raise TrustGraphError(f"Failed to compute draw ceiling: {exc}") from exc
