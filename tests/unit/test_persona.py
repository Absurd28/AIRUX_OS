"""Persona and companion tests: names, human lines, spoken phrasing, small talk without facts."""
from __future__ import annotations

import random

import pytest

from aira import persona as persona_mod
from aira.companion import Companion
from aira.llm import Completion, LLMError
from aira.persona import Persona, humanize_line, load_names
from aira.voice import spoken_text
from test_guard import make


def test_names_from_config_file_and_env(tmp_path, monkeypatch):
    monkeypatch.delenv("AIRA_USER_NAMES", raising=False)
    monkeypatch.delenv("AIRA_USER_NAME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(persona_mod, "gecos_first_name", lambda: [])
    assert load_names() == []
    monkeypatch.setattr(persona_mod, "gecos_first_name", lambda: ["Aryan"])
    assert load_names() == ["Aryan"]      # first name from the account, when nothing is configured
    (tmp_path / "aira").mkdir()
    (tmp_path / "aira" / "aira.toml").write_text('names = ["Aryan", "boss"]\n')
    assert load_names() == ["Aryan", "boss"]
    monkeypatch.setenv("AIRA_USER_NAMES", "Aryan, chief")
    assert load_names() == ["Aryan", "chief"]


def test_names_vary_and_none_means_no_name():
    p = Persona(["Aryan", "boss"], rng=random.Random(1))
    seen = {p.call() for _ in range(50)}
    assert seen == {"Aryan", "boss"}
    lines = {p.reminder("stretch") for _ in range(30)}
    assert len(lines) > 2 and all("stretch" in l for l in lines)
    assert any("Aryan" in l for l in lines) and any("boss" in l for l in lines)
    nobody = Persona([], rng=random.Random(1))
    assert all("," not in nobody.greeting(9).split("!")[0] for _ in range(10))
    assert "don't know the user's name" in nobody.prompt()
    assert 'Call them "Aryan" or "boss"' in p.prompt()


def test_fixed_lines_keep_the_rules():
    p = Persona(["Aryan", "boss"], rng=random.Random(3))
    for _ in range(20):
        assert p.about().startswith("I'm AIRA, your Adaptive Intelligent Research Assistant")
        assert "I'm not sure" in p.unsupported()       # traps rely on this phrase
        assert "Bella voice" in p.voice_changed("Bella")
    assert p.job_done("training run", "failed").startswith("Heads-up")   # no jokes on failures


@pytest.mark.parametrize("line,spoken", [
    ("Verified: ~/a.txt contains the text.", None),
    ("Sources: [1] ~/notes/lab.md", None),
    ("Not done: delete_path needs your confirmation and it was not given.",
     "I held off on deleting that -- it needs your OK first."),
    ("I couldn't verify that: clipboard failed: no clipboard tool available (install wl-clipboard or xclip)",
     "That didn't work: no clipboard tool available."),
    ("Not verified: run_command ran but its effect cannot be checked automatically.",
     "It ran, but I couldn't confirm that it worked."),
    ("The run diverged at step 400 [1].", "The run diverged at step 400."),
])
def test_humanize_line(line, spoken):
    assert humanize_line(line) == spoken


def test_spoken_text_vs_display():
    shown = "I've set it.\nVerified: item 3 is stored in the vault.\nNot done: browser needs your confirmation " \
            "and it was not given."
    assert spoken_text(shown) == "I've set it. I held off on using the browser -- it needs your OK first."


def test_emoji_not_spoken():
    assert spoken_text("Hope that helped, boss! 😄") == "Hope that helped, boss!"
    assert spoken_text("All set 🎉 for tomorrow.") == "All set for tomorrow."


def test_questions_are_not_done_claims():
    from aira import guard
    assert not guard.claims_done("I don't see any events tomorrow. Want me to help you set it up from scratch?")
    assert guard.claims_done("I set it up for you.")


def test_router_keeps_questions_about_the_user_out_of_about(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    orch, _, _ = make(home, tmp_path, [])
    assert '"about" ONLY when the user asks who or what AIRA is' in orch.router_prompt
    assert "what do you know about\n  me" in orch.router_prompt or "what do you know about me" in orch.router_prompt


@pytest.mark.parametrize("command,topic", [
    ("Who emailed me today?", "emailed"), ("Any new emails?", "emails"), ("Check my inbox", "inbox"),
    ("What's the weather like?", "weather"), ("What will Bitcoin cost tomorrow?", "bitcoin"),
    ("What is my bank balance?", "bank balance"), ("What's my manager's phone number?", "phone number"),
    ("Search the web for the weather in Pune", None), ("Open the browser and look up bitcoin", None),
    ("How much free disk space do I have?", None), ("Remind me to email Rao at 5pm", None),
    ("Remember that my bank account is with HDFC", None), ("Did anyone text me?", None),
    ("Did Rao reply to my email?", "email"),
])
def test_uncovered_topics(command, topic):
    from aira import guard
    got = guard.uncovered_topic(command)
    assert (got is None) == (topic is None)


def test_email_question_never_reaches_the_model(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    orch, llm, executed = make(home, tmp_path, [])   # the model must not be called at all
    turn = orch.handle("Who emailed me today?")
    assert turn.kind == "unsupported" and turn.reply.startswith("I'm not sure") and "web" not in turn.reply
    assert "can't see your email from here" in turn.reply
    t2 = orch.handle("What's the weather like?")
    assert t2.reply.startswith("I'm not sure") and "search the web" in t2.reply
    assert executed == []


class ScriptLLM:
    def __init__(self, texts):
        self.texts, self.calls = list(texts), []

    def chat(self, msgs, schema=None, **kw):
        self.calls.append(msgs)
        t = self.texts.pop(0)
        if isinstance(t, Exception):
            raise t
        return Completion("", {"text": t}, 0, 0, 0, 0)


def test_companion_reply_and_profile_in_prompt(tmp_path):
    from aira.memory.embed import HashEmbedder
    from aira.memory.vault import Vault
    v = Vault(tmp_path / "v.db", "k", embedder=HashEmbedder())
    v.add("profile", "favourite editor is Neovim")
    llm = ScriptLLM(["Doing great, boss! Neovim warmed up and ready?"])
    c = Companion(llm, Persona(["Aryan", "boss"]), v)
    text, events = c.reply("How are you doing?")
    assert text.startswith("Doing great") and events == []
    system = llm.calls[0][0]["content"]
    assert "favourite editor is Neovim" in system and "no tools" in system and "Never guess" in system


def test_companion_blocks_invented_facts(tmp_path):
    llm = ScriptLLM(["You have 3 meetings today and 42 GB free!", "Still: 3 meetings!"])
    text, events = Companion(llm, Persona(["Aryan"]), None).reply("How's it going?")
    assert "3" not in text and "42" not in text and "using a fixed line" in events[-1]
    llm = ScriptLLM(["Your disk is 90% full.", "All good here, Aryan. Want me to check anything?"])
    text, _ = Companion(llm, Persona(["Aryan"]), None).reply("how are you")
    assert text == "All good here, Aryan. Want me to check anything?"
    llm = ScriptLLM([LLMError("server down")])
    text, events = Companion(llm, Persona([]), None).reply("hi")
    assert text and "model unavailable" in events[0]


def test_router_smalltalk_goes_to_companion(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    orch, llm, executed = make(home, tmp_path, [{"chat": "smalltalk"}, {"text": "Long day? Want me to set a "
                                                                             "reminder to take a break?"}])
    turn = orch.handle("I'm so tired today")
    assert turn.kind == "chat" and "break" in turn.reply and executed == []
    assert "smalltalk" in str(orch.router_schema)
