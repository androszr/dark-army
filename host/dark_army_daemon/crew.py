"""The seven standard stage names. Stages have names, not character pools."""
ROLES = (
    "bc-card-preparer", "bc-planner", "bc-implementer", "bc-verifier",
    "bc-bug-auditor", "bc-integration-reviewer", "bc-security-reviewer",
)


def is_role(name: str) -> bool:
    return str(name or "").strip().lower() in ROLES
