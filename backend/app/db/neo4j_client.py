from neo4j import GraphDatabase

from backend.app.core.config import settings

_driver = None


def get_driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
    return _driver


def close_driver():
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None


def run_query(query: str, parameters: dict | None = None):
    """Run a read query and return list of record dicts."""
    driver = get_driver()
    with driver.session() as session:
        result = session.run(query, parameters or {})
        return [record.data() for record in result]


def run_write(query: str, parameters: dict | None = None):
    """Run a write query."""
    driver = get_driver()
    with driver.session() as session:
        session.run(query, parameters or {})
