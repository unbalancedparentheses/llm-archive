import math
import re
from collections import Counter

# Common stop words to filter out
STOP_WORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "out", "off", "over",
    "under", "again", "further", "then", "once", "here", "there", "when",
    "where", "why", "how", "all", "both", "each", "few", "more", "most",
    "other", "some", "such", "no", "nor", "not", "only", "own", "same",
    "so", "than", "too", "very", "just", "don", "now", "also", "about",
    "up", "its", "it", "this", "that", "these", "those", "i", "me", "my",
    "we", "our", "you", "your", "he", "him", "his", "she", "her", "they",
    "them", "their", "what", "which", "who", "whom", "if", "or", "and",
    "but", "because", "while", "until", "although", "though", "since",
    "like", "get", "got", "make", "made", "let", "want", "need", "use",
    "used", "using", "try", "know", "think", "see", "look", "way", "thing",
    "things", "something", "anything", "everything", "nothing", "going",
    "one", "two", "first", "new", "right", "well", "even", "back", "still",
    "much", "many", "really", "sure", "yes", "yeah", "ok", "okay",
    # LLM conversation noise
    "file", "code", "function", "error", "run", "add", "change", "update",
    "please", "thanks", "thank", "help", "work", "working", "works",
    "command", "output", "input", "data", "value", "type", "name",
}

_WORD_RE = re.compile(r"[a-z][a-z0-9_-]{2,20}")


def _tokenize(text: str) -> list[str]:
    words = _WORD_RE.findall(text.lower())
    return [w for w in words if w not in STOP_WORDS]


def extract_topics(conversations: list[dict], top_n: int = 30) -> list[dict]:
    """
    Extract topics from conversations using TF-IDF.
    Each conversation is treated as a document.
    conversations: list of {"project": str, "text": str}
    Returns: list of {"topic": str, "score": float, "projects": list[str], "count": int}
    """
    if not conversations:
        return []

    # Build document term frequencies
    doc_tfs = []
    for conv in conversations:
        tokens = _tokenize(conv["text"])
        tf = Counter(tokens)
        doc_tfs.append(tf)

    # Build IDF
    n_docs = len(conversations)
    all_terms = set()
    for tf in doc_tfs:
        all_terms.update(tf.keys())

    idf = {}
    for term in all_terms:
        doc_count = sum(1 for tf in doc_tfs if term in tf)
        idf[term] = math.log(n_docs / (1 + doc_count))

    # Score each term by sum of TF-IDF across all docs
    term_scores = Counter()
    term_projects = {}
    term_doc_count = Counter()

    for i, tf in enumerate(doc_tfs):
        project = conversations[i]["project"]
        for term, count in tf.items():
            score = count * idf.get(term, 0)
            term_scores[term] += score
            if term not in term_projects:
                term_projects[term] = set()
            term_projects[term].add(project)
            term_doc_count[term] += 1

    # Filter: must appear in at least 2 conversations
    results = []
    for term, score in term_scores.most_common(top_n * 2):
        if term_doc_count[term] < 2:
            continue
        results.append({
            "topic": term,
            "score": round(score, 1),
            "projects": sorted(term_projects[term]),
            "count": term_doc_count[term],
        })
        if len(results) >= top_n:
            break

    return results
