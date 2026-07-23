"""Chat protocol parsing: Twitch IRC, Kick frames, channel normalization."""

import json

from video_stream.chat import (
    KICK_EMOTE_RE,
    PRIVMSG_RE,
    COLOR_RE,
    NAME_RE,
    _unescape_tag,
    normalize_channel,
)


def test_normalize_channel_strips_urls_hashes_and_junk():
    assert normalize_channel("twitch", "  #CoolStreamer ") == "coolstreamer"
    assert normalize_channel("twitch", "https://www.twitch.tv/CoolStreamer") == "coolstreamer"
    assert normalize_channel("twitch", "twitch.tv/foo?ref=x") == "foo"
    assert normalize_channel("twitch", "twitch.tv/foo/") == "foo"
    assert normalize_channel("kick", "https://kick.com/SomeGuy") == "someguy"
    assert normalize_channel("kick", "12345") == "12345"


def test_privmsg_parse_with_tags():
    line = (
        "@badge-info=;color=#FF69B4;display-name=CoolCat;subscriber=0 "
        ":coolcat!coolcat@coolcat.tmi.twitch.tv PRIVMSG #somechannel :hello world"
    )
    m = PRIVMSG_RE.match(line)
    assert m is not None
    tags, nick, text = m.group(1), m.group(2), m.group(3)
    assert nick == "coolcat"
    assert text == "hello world"
    assert COLOR_RE.search(tags).group(1) == "#FF69B4"
    assert NAME_RE.search(tags).group(1) == "CoolCat"


def test_privmsg_parse_without_tags():
    line = ":plainuser!plainuser@x.tmi.twitch.tv PRIVMSG #chan :no tags here"
    m = PRIVMSG_RE.match(line)
    assert m is not None
    assert m.group(1) is None
    assert m.group(2) == "plainuser"
    assert m.group(3) == "no tags here"


def test_action_me_message_strips_ctcp():
    text = "\x01ACTION waves hello\x01"
    import re

    if text and text[0] == "\x01":
        text = re.sub(r"^ACTION ", "", text[1:-1])
    assert text == "waves hello"


def test_tag_unescape():
    assert _unescape_tag(r"Cool\sCat") == "Cool Cat"
    assert _unescape_tag(r"semi\:colon") == "semi;colon"
    assert _unescape_tag(r"back\\slash") == "back\\slash"


def test_kick_emote_markup_replaced_with_name():
    content = "nice [emote:12345:KEKW] play [emote:9:pog]"
    assert KICK_EMOTE_RE.sub(r"\1", content) == "nice KEKW play pog"


def test_kick_chat_event_double_decode():
    frame = {
        "event": "App\\Events\\ChatMessageEvent",
        "data": json.dumps(
            {
                "sender": {"username": "kicker", "identity": {"color": "#75FD46"}},
                "content": "hello [emote:1:wave]",
            }
        ),
    }
    assert frame["event"] == "App\\Events\\ChatMessageEvent"
    data = json.loads(frame["data"])
    assert data["sender"]["username"] == "kicker"
    assert data["sender"]["identity"]["color"] == "#75FD46"
    assert KICK_EMOTE_RE.sub(r"\1", data["content"]) == "hello wave"
