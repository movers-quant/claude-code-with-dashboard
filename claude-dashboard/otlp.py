"""OTLP/HTTP JSON metrics parser for Claude Code telemetry."""

from config import MAX_SESSIONS, VALID_TOKEN_TYPES
from sessions import token_metrics


def parse_otlp_metrics(body: dict) -> None:
    """Parse an OTLP metric payload and update token_metrics in-place."""
    for rm in body.get("resourceMetrics", []):
        session_id = None
        for attr in (rm.get("resource") or {}).get("attributes", []):
            if attr.get("key") in ("session.id", "claude.session_id"):
                session_id = (attr.get("value") or {}).get("stringValue")
                break

        for sm in rm.get("scopeMetrics", []):
            for metric in sm.get("metrics", []):
                name = metric.get("name")
                if name not in ("claude_code.token.usage", "claude_code.cost.usage"):
                    continue

                data_points = (
                    (metric.get("sum") or {}).get("dataPoints")
                    or (metric.get("gauge") or {}).get("dataPoints")
                    or []
                )
                for dp in data_points:
                    attrs = {}
                    for attr in dp.get("attributes", []):
                        v = attr.get("value", {})
                        attrs[attr["key"]] = (
                            v.get("stringValue") or v.get("intValue") or v.get("doubleValue")
                        )

                    sid = session_id or attrs.get("session.id") or attrs.get("claude.session_id")
                    if not sid:
                        continue

                    if len(token_metrics) >= MAX_SESSIONS and sid not in token_metrics:
                        continue

                    if sid not in token_metrics:
                        token_metrics[sid] = {
                            "input": 0,
                            "output": 0,
                            "cache_read": 0,
                            "cache_creation": 0,
                            "cost": 0,
                        }
                    m = token_metrics[sid]
                    val = int(dp.get("asInt") or 0) or float(dp.get("asDouble") or 0)

                    if name == "claude_code.token.usage":
                        token_type = attrs.get("type") or attrs.get("token.type") or "input"
                        if token_type in VALID_TOKEN_TYPES:
                            m[token_type] += val
                    elif name == "claude_code.cost.usage":
                        m["cost"] += val
