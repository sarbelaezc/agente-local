from ai.context_builder import ChatMessage, build_context


def test_context_is_limited_to_recent_messages():
    messages = [
        ChatMessage("A", "one", "1"),
        ChatMessage("B", "two", "2"),
        ChatMessage("C", "three", "3"),
    ]
    assert build_context(messages, 2) == "[2] B: two\n[3] C: three"
