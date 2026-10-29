"""Parser regression tests for the Telegram fast-path (no network calls)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import main  # noqa: E402

main.CFG = main.load_config()

WAKE_CASES = [
    ("أنا عايزك تصحيني بعد 3 ساعات واعمل منبه", 180),
    ("صحيني بعد ٣ ساعات", 180),
    ("اعمل منبه بعد ساعتين", 120),
    ("نبهني بعد نص ساعة", 30),
    ("صحيني بعد ساعة ونص", 90),
    ("wake me in 45 minutes", 45),
    ("set an alarm in 2 hours", 120),
    ("اعمل منبه بعد 30 دقيقة", 30),
]

FILE_CASES = [
    ("اعمل فايل في الديسكتوب اسمه othvm.TEST", "othvm.TEST"),
    ("اعمل ملف على الديسكتوب اسمه report.txt", "report.txt"),
    ("create a file on the desktop named notes.md", "notes.md"),
    ("اعمل فايل في الديسكتوب اسمه «data.json»", "data.json"),
]

NEGATIVE = [
    "إيه الأخبار النهاردة؟",
    "اعمل منبه",  # no time -> agent should ask
    "افتح الديسكتوب",
    "اعمل فايل اسمه test.txt",  # not desktop -> agent handles it
    "قولي الساعة كام",
    "امسح فايل من الديسكتوب اسمه test.txt",  # destructive -> agent
    "الغي المنبه بعد 3 ساعات",  # cancellation -> agent
    "delete the file on the desktop named notes.md",
]

failures = 0
for text, expected in WAKE_CASES:
    got = main.parse_wake(text)
    minutes = (got or {}).get("start_in_minutes")
    status = "ok" if minutes == expected else "FAIL"
    if status == "FAIL":
        failures += 1
    print(f"{status:4} wake {minutes!r:>6} expected {expected!r:>6} :: {text}")

for text, expected in FILE_CASES:
    got = main.parse_file(text)
    name = (got or {}).get("name")
    status = "ok" if name == expected else "FAIL"
    if status == "FAIL":
        failures += 1
    print(f"{status:4} file {name!r} expected {expected!r} :: {text}")

for text in NEGATIVE:
    got = main.parse_wake(text) or main.parse_file(text)
    status = "ok" if got is None else "FAIL"
    if status == "FAIL":
        failures += 1
    print(f"{status:4} none {got!r} :: {text}")

print("FAILURES", failures)
sys.exit(1 if failures else 0)
