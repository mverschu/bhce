#!/usr/bin/env python3
"""Merge BloodHoundQueryLibrary YAML queries into lib/cq.json.

The script is safe to run again. It reads the current cq.json plus the
library, drops whitespace-duplicate Cypher, and rewrites only the legacy
category names.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CQ_PATH = ROOT / "lib" / "cq.json"
LIBRARY_DIR = ROOT / "BloodHoundQueryLibrary" / "queries"

OFFICIAL_CATEGORIES = {
    "Domain Information",
    "AD Sites",
    "Dangerous Privileges",
    "Kerberos Interaction",
    "Shortest Paths",
    "Active Directory Certificate Services",
    "Active Directory Hygiene",
    "NTLM Relay Attacks",
    "General",
    "Azure Hygiene",
    "Microsoft Graph",
    "Cross Platform Attack Paths",
}

LEGACY_CATEGORIES = {
    "User Information": "Domain Information",
    "Domain Escalation": "Shortest Paths",
    "OS Finder": "Domain Information",
    "Admin Hunter": "Dangerous Privileges",
    "Azure": "Azure Hygiene",
    "GPO/Group Information": "Domain Information",
    "PlainText Password Queries": "Active Directory Hygiene",
    "Group Hunts": "Domain Information",
    "Delegation Attacks": "Kerberos Interaction",
    "Certificates": "Active Directory Certificate Services",
    "Password Hunts": "Active Directory Hygiene",
    "Owned Hunter": "Shortest Paths",
    "Password Last Set": "Active Directory Hygiene",
    "Latteral Movement": "Shortest Paths",
    "Lateral Movement": "Shortest Paths",
    "Configuration Weakness": "Active Directory Hygiene",
}

# Legacy pickers cannot run as a single saved query. These replace the
# multi-step queryList with one statement and keep the reason in description.
CANONICAL = {
    "show enrollment rights for certificate template": (
        "MATCH p=(g)-[:Enroll|AutoEnroll]->(n:GPO)\n"
        "WHERE n.type = 'Certificate Template'\n"
        "RETURN p",
        "Enrollment rights on certificate templates. The legacy query asked you to pick a template; this version returns every template.",
    ),
    "show rights for certificate authority": (
        "MATCH p=(g)-[:ManageCa|ManageCertificates|Auditor|Operator|Read|Enroll]->(n:GPO)\n"
        "WHERE n.type = 'Enrollment Service'\n"
        "RETURN p",
        "Rights on certificate authorities. The legacy query asked you to pick an authority; this version returns every enrollment service.",
    ),
    "find shortest path from owned node to domain admin": (
        "MATCH (m:Group)\n"
        "WHERE m.name =~ '(?i).*DOMAIN ADMINS.*'\n"
        "MATCH p=shortestPath((n:User {owned:true})-[r:MemberOf|AdminTo|HasSession|Contains|GpLink|Owns|DCSync|AllExtendedRights|ForceChangePassword|GenericAll|GenericWrite|WriteDacl|WriteOwner*1..]->(m))\n"
        "RETURN p",
        "Shortest path from an owned user to a Domain Admins group. The legacy query asked you to pick the group; this version matches groups named like Domain Admins.",
    ),
    "find all sessions a user in a specific domain has": (
        "MATCH p=(m:Computer)-[r:HasSession]->(n:User) RETURN p",
        "Sessions from computers to users. The legacy query asked you to pick a domain; this version returns sessions in every domain.",
    ),
    "find an object from domain 'a' that can do anything to a foreign object": (
        "MATCH p=(n)-[r]->(d)\nWHERE NOT d.domain = n.domain\nRETURN p",
        "Objects with a relationship to an object in another domain. The legacy query asked you to pick the source domain; this version returns every cross-domain relationship.",
    ),
}


def normalize_cypher(query: str) -> str:
    return re.sub(r"\s+", " ", query).strip()


def map_category(category: str) -> str:
    if category in OFFICIAL_CATEGORIES:
        return category
    if category in LEGACY_CATEGORIES:
        return LEGACY_CATEGORIES[category]
    if category.startswith("Dangerous Privileges"):
        return "Dangerous Privileges"
    raise SystemExit(f"Unknown query category: {category}")


def inline_props(query: str, props: dict | None) -> str:
    if not props:
        return query
    for key, value in props.items():
        literal = "'" + str(value).replace("'", "''") + "'"
        query = re.sub(rf"\${re.escape(str(key))}\b", literal, query)
    return query


def final_step(query_list: list[dict]) -> dict:
    for step in query_list:
        if step.get("final"):
            return step
    if not query_list:
        raise SystemExit("Query is missing queryList")
    return query_list[-1]


def entry(name: str, category: str, cypher: str, description: str, source: str) -> dict:
    cypher = cypher.strip()
    if not cypher:
        raise SystemExit(f"Empty Cypher for {name}")
    return {
        "name": name.strip(),
        "category": map_category(category),
        "cypher": cypher,
        "description": (description or "").strip(),
        "source": source,
    }


def from_legacy(raw: dict) -> dict:
    name = raw["name"].strip()
    canonical = CANONICAL.get(name.casefold())
    if canonical:
        cypher, description = canonical
        return entry(name, raw.get("category", ""), cypher, description, "legacy")

    step = final_step(raw.get("queryList") or [])
    cypher = inline_props(step.get("query") or "", step.get("props"))
    if "$result" in cypher or any("$result" in (item.get("query") or "") for item in raw.get("queryList") or []):
        raise SystemExit(f"Unresolved picker ($result) in {name}")
    return entry(name, raw.get("category", ""), cypher, raw.get("description") or "", "legacy")


def from_library(path: Path) -> dict:
    document = yaml.safe_load(path.read_text())
    return entry(
        document["name"],
        document.get("category") or "",
        document["query"],
        document.get("description") or "",
        "library",
    )


def prefer(current: dict, incoming: dict) -> dict:
    if incoming["source"] == "library" and current["source"] != "library":
        return incoming
    if current["source"] == "library" and incoming["source"] != "library":
        return current
    if incoming["description"] and not current["description"]:
        return incoming
    return current


def is_broad_spn(cypher: str) -> bool:
    compact = re.sub(r"\s+", "", cypher).lower()
    return compact in {
        "match(n{hasspn:true})returnn",
        "match(n{hasspn:true})returnnlimit100",
    }


def target_suffix(cypher: str) -> str | None:
    has_user = re.search(r"\(n:User\)", cypher) is not None
    has_computer = re.search(r"\(n:Computer\)", cypher) is not None
    if has_user and not has_computer:
        return " over Users"
    if has_computer and not has_user:
        return " over Computers"
    return None


def disambiguate(queries: list[dict]) -> list[str]:
    """Rename case-insensitive name collisions. Returns the rename log."""
    notes: list[str] = []
    grouped: dict[str, list[dict]] = {}
    for query in queries:
        grouped.setdefault(query["name"].casefold(), []).append(query)

    for group in grouped.values():
        if len(group) < 2:
            continue
        for query in group:
            if query["name"].casefold() == "all kerberoastable users" and is_broad_spn(query["cypher"]):
                old = query["name"]
                query["name"] = "All objects with an SPN (hasspn)"
                notes.append(f"{old} -> {query['name']}")

    grouped = {}
    for query in queries:
        grouped.setdefault(query["name"].casefold(), []).append(query)

    for group in grouped.values():
        if len(group) < 2:
            continue
        for query in group:
            suffix = target_suffix(query["cypher"])
            if suffix and not query["name"].endswith(suffix):
                old = query["name"]
                query["name"] = query["name"] + suffix
                notes.append(f"{old} -> {query['name']}")

    grouped = {}
    for query in queries:
        grouped.setdefault(query["name"].casefold(), []).append(query)
    for group in grouped.values():
        if len(group) < 2:
            continue
        for index, query in enumerate(group, start=1):
            old = query["name"]
            query["name"] = f"{old} ({index})"
            notes.append(f"{old} -> {query['name']}")
    return notes


def leftover_parameter(cypher: str) -> str | None:
    match = re.search(r"\$(?:result|sid|name)\b", cypher)
    return match.group(0) if match else None


def to_document(query: dict) -> dict:
    document = {
        "name": query["name"],
        "category": query["category"],
    }
    if query["description"]:
        document["description"] = query["description"]
    document["queryList"] = [{"final": True, "query": query["cypher"]}]
    return document


def main() -> int:
    if not LIBRARY_DIR.is_dir():
        raise SystemExit(f"Query library not found: {LIBRARY_DIR}")

    current = json.loads(CQ_PATH.read_text())
    legacy = [from_legacy(query) for query in current["queries"]]
    library = [from_library(path) for path in sorted(LIBRARY_DIR.glob("*.yml"))]

    merged: dict[str, dict] = {}
    dropped = 0
    for query in [*legacy, *library]:
        key = normalize_cypher(query["cypher"])
        existing = merged.get(key)
        if existing is None:
            merged[key] = query
            continue
        dropped += 1
        merged[key] = prefer(existing, query)

    queries = list(merged.values())
    renames = disambiguate(queries)

    seen_names: set[str] = set()
    seen_cypher: set[str] = set()
    for query in queries:
        name_key = query["name"].casefold()
        cypher_key = normalize_cypher(query["cypher"])
        if name_key in seen_names:
            raise SystemExit(f"Duplicate name remains: {query['name']}")
        if cypher_key in seen_cypher:
            raise SystemExit(f"Duplicate Cypher remains: {query['name']}")
        seen_names.add(name_key)
        seen_cypher.add(cypher_key)
        parameter = leftover_parameter(query["cypher"])
        if parameter:
            raise SystemExit(f"{query['name']} still contains {parameter}")
        if query["category"] not in OFFICIAL_CATEGORIES:
            raise SystemExit(f"{query['name']} has category {query['category']}")

    queries.sort(key=lambda query: (query["category"].casefold(), query["name"].casefold()))
    CQ_PATH.write_text(json.dumps({"queries": [to_document(query) for query in queries]}, indent="\t", ensure_ascii=False) + "\n")

    print(f"legacy {len(legacy)}")
    print(f"library {len(library)}")
    print(f"dropped {dropped}")
    print(f"written {len(queries)}")
    for note in renames:
        print(f"rename {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
