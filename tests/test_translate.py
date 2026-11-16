import asyncio
import importlib

import pytest


@pytest.fixture
def tr(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("TRANSLATE_MODEL", "test-model")
    import translate
    importlib.reload(translate)
    return translate


def test_split_target(tr):
    assert tr.split_target("Arabic: the tap is leaking") == ("Arabic", "the tap is leaking")
    assert tr.split_target("عربی: شیر آب چکه می‌کند") == ("عربی", "شیر آب چکه می‌کند")
    assert tr.split_target("Gingali\ncan you come at 10?") == ("Gingali", "can you come at 10?")
    assert tr.split_target("Arabic food was great today") is None
    assert tr.split_target("hello there my good friend: how are you") is None  # head over 3 words
    assert tr.split_target("Arabic:") is None


def test_needs_translation(tr, monkeypatch):
    assert tr.needs_translation("ara", 0.98)
    assert tr.needs_translation("ben", 0.9)
    assert not tr.needs_translation("fas", 0.99)
    assert not tr.needs_translation("eng", 0.99)
    assert not tr.needs_translation("ara", 0.3)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert not tr.needs_translation("ara", 0.98)


def test_plain_text_never_calls_the_model(tr, monkeypatch):
    async def boom(*a):
        raise AssertionError("model called")
    monkeypatch.setattr(tr, "_ask", boom)
    plan = asyncio.run(tr.plan_outgoing("Arabic food was great today"))
    assert plan.speak == "Arabic food was great today" and not plan.show and not plan.ask


def test_no_key_means_no_translation(tr, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")

    async def boom(*a):
        raise AssertionError("model called")
    monkeypatch.setattr(tr, "_ask", boom)
    plan = asyncio.run(tr.plan_outgoing("Arabic: the tap is leaking"))
    assert plan.speak == "Arabic: the tap is leaking" and not plan.show


@pytest.mark.parametrize("answer, expect", [
    ({"status": "translate", "language": "Arabic", "translation": "الحنفية تسرب"}, ("الحنفية تسرب", "الحنفية تسرب", "")),
    ({"status": "not_language", "language": "", "translation": ""}, ("Hi\nhow are you", "", "")),
])
def test_outgoing_plans(tr, monkeypatch, answer, expect):
    async def fake(system, user, schema):
        return answer
    monkeypatch.setattr(tr, "_ask", fake)
    text = "Arabic: the tap is leaking" if answer["status"] == "translate" else "Hi\nhow are you"
    plan = asyncio.run(tr.plan_outgoing(text))
    assert (plan.speak, plan.show, plan.ask) == expect


def test_unsure_asks_once(tr, monkeypatch):
    async def fake(system, user, schema):
        return {"status": "unsure", "language": "", "translation": ""}
    monkeypatch.setattr(tr, "_ask", fake)
    plan = asyncio.run(tr.plan_outgoing("Gx: come at 10"))
    assert plan.ask.startswith('Which language did you mean by "Gx"?') and not plan.speak


def test_incoming_layout_and_failure(tr, monkeypatch):
    async def fake(system, user, schema):
        return {"english": "The tap is leaking.", "persian": "شیر آب چکه می‌کند."}
    monkeypatch.setattr(tr, "_ask", fake)
    out = asyncio.run(tr.explain_incoming("الحنفية تسرب"))
    assert out == "الحنفية تسرب\n\n🇬🇧 English\nThe tap is leaking.\n\n🇮🇷 Persian\nشیر آب چکه می‌کند."

    async def fail(system, user, schema):
        raise tr.TranslateError("refused")
    monkeypatch.setattr(tr, "_ask", fail)
    assert asyncio.run(tr.explain_incoming("x")) == "x\n\n(Could not translate this one.)"
