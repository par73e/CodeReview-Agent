"""Extract database table relationships and SQL risk facts."""

import re
from typing import List

from ....types import SourceFile
from ..evidence import EvidenceEdge, EvidenceGraph, EvidenceLocation, EvidenceNode


TABLE_PATTERNS = {
    "select": re.compile(r"\bfrom\s+([`\w.]+)", re.I),
    "insert": re.compile(r"\binsert\s+into\s+([`\w.]+)", re.I),
    "update": re.compile(r"\bupdate\s+([`\w.]+)", re.I),
    "delete": re.compile(r"\bdelete\s+from\s+([`\w.]+)", re.I),
}


def extract_sql(files: List[SourceFile], graph: EvidenceGraph) -> None:
    del files
    for sql_node in list(graph.nodes.values()):
        if sql_node.kind != "sql_statement":
            continue
        operation = str(sql_node.metadata.get("operation", "")).lower()
        statement = str(sql_node.metadata.get("statement", ""))
        pattern = TABLE_PATTERNS.get(operation)
        table_names = []
        if pattern:
            table_names = [match.group(1).strip("`") for match in pattern.finditer(statement)]
        sql_node.metadata["tables"] = list(dict.fromkeys(table_names))
        sql_node.metadata["uses_dollar_placeholder"] = "${" in statement
        sql_node.metadata["uses_select_star"] = bool(re.search(r"\bselect\s+\*", statement, re.I))
        sql_node.metadata["has_where"] = bool(re.search(r"\bwhere\b", statement, re.I))
        sql_node.metadata["has_pagination"] = bool(re.search(r"\blimit\b|\boffset\b", statement, re.I))
        for table_name in sql_node.metadata["tables"]:
            table = EvidenceNode(
                "table:" + str(table_name).lower(),
                "database_table",
                str(table_name),
                EvidenceLocation(sql_node.location.file, sql_node.location.line),
                {},
            )
            graph.add_node(table)
            relation = "reads_table" if operation == "select" else "writes_table"
            graph.add_edge(EvidenceEdge(sql_node.node_id, table.node_id, relation, "SQL 明确包含表名 " + str(table_name), "high", sql_node.location, table.location))
