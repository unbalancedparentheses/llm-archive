from collections import defaultdict


def _trigrams(text: str) -> set[str]:
    text = text.lower()
    return {text[i : i + 3] for i in range(len(text) - 2)}


def _similarity(a: str, b: str) -> float:
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def find_recurring(messages: list[dict], threshold: float = 0.4, min_cluster: int = 3) -> list[dict]:
    # Group similar user messages using single-linkage clustering
    clusters: list[list[dict]] = []

    for msg in messages:
        text = msg["content"][:200]  # compare first 200 chars
        if len(text) < 20:
            continue

        placed = False
        for cluster in clusters:
            # Compare against first message in cluster (representative)
            if _similarity(text, cluster[0]["content"][:200]) >= threshold:
                cluster.append(msg)
                placed = True
                break

        if not placed:
            clusters.append([msg])

    # Filter to clusters with enough occurrences
    results = []
    for cluster in clusters:
        if len(cluster) < min_cluster:
            continue

        projects = set(m["project"] for m in cluster)
        results.append({
            "count": len(cluster),
            "projects": sorted(projects),
            "example": cluster[0]["content"][:200],
            "first": cluster[0]["timestamp"],
            "last": cluster[-1]["timestamp"],
        })

    results.sort(key=lambda x: x["count"], reverse=True)
    return results
