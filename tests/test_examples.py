from app import ollama

LABELS = {
    t: {"title": t, "description": d}
    for t, d in [
        ("effort:S", ""),
        ("effort:L", ""),
        ("computer", "Context - at a computer"),
        ("waiting_for", "Flag - blocked on someone"),
    ]
}


def _task(title, *labels):
    return {"title": title, "labels": [{"title": t} for t in labels] or None}


def test_select_examples_keeps_only_rule_satisfying_tasks():
    completed = [
        _task("Refactor auth", "computer", "effort:L"),
        _task("Untagged"),
        _task("Only effort", "effort:S"),
        _task("Waiting on plumber", "waiting_for"),
    ]
    assert ollama.select_examples(completed, LABELS) == [
        ("Refactor auth", ["computer", "effort:L"]),
        ("Waiting on plumber", ["waiting_for"]),
    ]


def test_examples_block_lists_each_example():
    block = ollama._build_examples_block([("Refactor auth", ["computer", "effort:L"])])
    assert "'Refactor auth' -> ['computer', 'effort:L']" in block
    assert "take precedence" in block
