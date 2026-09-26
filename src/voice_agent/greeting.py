"""The agent's opening line, spoken like any other reply."""

from voice_agent import timing
from voice_agent.channel import Channel
from voice_agent.conversation import Conversation
from voice_agent.heard import Spoken
from voice_agent.tts import TTS
from voice_agent.turn import Speech


async def greet(
    channel: Channel, conversation: Conversation, speaker: TTS | None, text: str
) -> Spoken | None:
    """Greet a conversation that has not started yet. Returns the greeting's
    voice, which the user can talk over like any other."""
    # Nothing awaited between this check and the append: two tabs opening the
    # same link would otherwise both pass it and both greet.
    if not text or conversation.messages or conversation.ended:
        return None
    message = conversation.add_assistant(text)
    conversation.opening = message
    await channel.send_json({"type": "greeting", "text": text})
    if speaker is None:
        return None
    speech = Speech(channel, speaker, timing.now(), Spoken(message=message))
    speech.say(text)
    speech.finish()
    try:
        await speech.done()
    finally:
        await speech.cancel()  # a no-op once done; stops synthesis if the socket went
    return speech.voice
