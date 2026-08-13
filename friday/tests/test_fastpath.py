"""
tests/test_fastpath.py
Tier 1: what matches, and — more importantly — what must not.

    python3 tests/test_fastpath.py

MATCHING IS TESTED, ANSWERING IS NOT. match() is pure: no config, no
connection, no network. That is why the near-miss table below can be this
long, and the near-miss table is the point of the file. A missed match costs a
model call, which is what the message cost yesterday; a WRONG match is a
confident wrong answer with no model in the loop to hedge it.

THE PHRASINGS ARE REAL. Every line in the match table below was actually sent
to Friday and recovered from logs/friday.log, lightly edited where it carried
anything personal. They are here rather than the corpus file itself because
183 real messages include names, appointments and a locker combination, and a
test fixture is not the place for those.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from router import fastpath  # noqa: E402

failures = []


def check(label, cond):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


def matches(text, pattern, *args):
    m = fastpath.match(text)
    ok = m is not None and m.pattern == pattern and (not args or m.args == args)
    got = f"{m.pattern}{m.args}" if m else "None"
    check(f"{text!r} -> {pattern}" + (f"{args}" if args else "") + f"  (got {got})", ok)


def falls_through(text):
    m = fastpath.match(text)
    check(f"{text!r} FALLS THROUGH  (got {m.pattern if m else 'None'})", m is None)


print("\n── greeting ──")
for t in ["hello", "Hello.", "hi", "Hey", "hey sir", "good morning",
          "Good evening.", "are you there?", "Are you there, sir?",
          "you there", "sir", "Friday", "are you awake?"]:
    matches(t, "greeting")

print("\n── brief ──")
for t in ["brief me", "Brief me now.", "brief", "morning briefing",
          "what's my day", "whats my day look like", "how does today look"]:
    matches(t, "brief")

print("\n── weather ──")
for t in ["will it rain today?", "Will it rain tonight?", "is it going to rain today?",
          "what's the weather?", "whats the weather like?", "what is the weather like",
          "weather", "rain", "will it rain at 3pm today?", "will it rain today at 3pm?",
          "what is the chance of rain in the next 12 hours?",
          "will there be precipitation today?", "how hot is it outside",
          "what is the temperature right now"]:
    matches(t, "weather")

print("\n── calendar (today/tomorrow ONLY) ──")
matches("what is on my calendar today?", "calendar", "today")
matches("What's on my calendar tomorrow?", "calendar", "tomorrow")
matches("whats on my calendar tomorrow", "calendar", "tomorrow")
matches("what do I have on my calendar today", "calendar", "today")
matches("what do i have tomorrow", "calendar", "tomorrow")
matches("what am I doing today", "calendar", "today")
matches("my schedule tomorrow", "calendar", "tomorrow")
matches("list everything on my calendar today", "calendar", "today")
matches("today's schedule", "calendar", "today")

print("\n── pause ──")
matches("pause", "pause")
matches("Pause.", "pause")

print("\n── NEAR MISSES: these must reach the model ──")
# A pattern nearly matching is the case the whole fullmatch design exists for.
falls_through("Hey Jarvis, what's the weather?")     # greeting-prefixed, not a greeting
falls_through("hello, can you add lunch tomorrow")   # greeting-prefixed, not a greeting
falls_through("Good morning. Give me one sentence on how today looks.")
falls_through("should I bring a jacket to practice?")   # judgement, not weather
falls_through("where is the weather from?")             # meta, not weather
falls_through("what is the percent chance of rain according to your data?")
falls_through("does it rain a lot here in August?")     # climate, not forecast
falls_through("what's on my calendar")                  # NO DATE — never guess one
falls_through("what's on my calendar on August 24th?")  # explicit date, not today/tomorrow
falls_through("what am I doing next week?")
falls_through("what am I doing on Friday?")
falls_through("brief me on the robotics meeting")       # not the briefing
falls_through("pause the music")
falls_through("resume")            # unreachable by design — see _pause()'s docstring
falls_through("add team dinner tomorrow at 6:30pm")
falls_through("I have work on Wednesday from 6-9 pm")
falls_through("what is 3 times 7")
falls_through("")
falls_through("   ")

print("\n── a match never swallows a second clause ──")
# search() instead of fullmatch() here would answer the weather half and drop
# the calendar write silently, which is the worst thing this module could do.
falls_through("add lunch tomorrow, and what's the weather")
falls_through("weather, and add a dentist appointment on the 20th")

print("\n── normalisation is shallow on purpose ──")
matches("HELLO", "greeting")
matches("  hello  ", "greeting")
matches("hello!!!", "greeting")
matches("what’s the weather?", "weather")     # curly apostrophe
falls_through("helloooo")                          # not a spelling correction service

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("test_fastpath: all checks passed")
