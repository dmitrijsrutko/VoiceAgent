from voice_agent.conversation import Conversation, Message


def test_messages_accumulate_in_order() -> None:
    conversation = Conversation(id="k")
    conversation.add_user("hello")
    conversation.add_assistant("hi there")
    conversation.add_user("how are you")

    assert conversation.messages == [
        Message("user", "hello"),
        Message("assistant", "hi there"),
        Message("user", "how are you"),
    ]


def test_a_conversation_starts_open_and_can_be_ended() -> None:
    conversation = Conversation(id="k")
    assert not conversation.ended
    conversation.end()
    assert conversation.ended
