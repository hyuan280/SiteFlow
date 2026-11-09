"""Saved workflow policy is independent of the discovery browser's proxy."""


def saved_lane(target) -> str:
    policy = target.get("run_lane_policy", "auto")
    if policy not in {"auto", "general", "interaction", "proxy_general", "proxy_interaction"}:
        raise ValueError("Invalid saved workflow lane policy")
    return policy
