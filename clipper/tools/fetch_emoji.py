"""Download the Twemoji (openly-licensed) PNG set used by the dashboard emoji picker.

Run once: python tools/fetch_emoji.py
Saves 72x72 PNGs to C:\\Users\\Brett\\clipforge\\emoji\\<codepoint>.png
Keep EMOJIS in sync with the EMOJIS list in dashboard.html.
"""
import os
import urllib.request

EMOJIS = ["😂", "🤣", "💀", "😭", "😱", "😳", "😬", "😎", "🥶", "🤯", "😤", "🤔", "👀", "🙌",
          "👏", "👇", "👉", "👆", "🔥", "💯", "✨", "⭐", "💥", "🏆", "🎯", "✅", "❌", "⚠️",
          "❤️", "🎮", "🐔", "🧠", "👑", "🚨", "😅", "🤡"]
OUT = r"C:\Users\Brett\clipforge\emoji"
BASE = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/"


def codepoint(s: str) -> str:
    return "-".join(format(ord(c), "x") for c in s if ord(c) != 0xFE0F)


def main():
    os.makedirs(OUT, exist_ok=True)
    ok = fail = 0
    for e in EMOJIS:
        cp = codepoint(e)
        dst = os.path.join(OUT, cp + ".png")
        if os.path.exists(dst):
            ok += 1
            continue
        try:
            req = urllib.request.Request(BASE + cp + ".png", headers={"User-Agent": "clipforge"})
            with urllib.request.urlopen(req, timeout=20) as r, open(dst, "wb") as f:
                f.write(r.read())
            ok += 1
        except Exception as ex:  # noqa: BLE001
            print("FAIL", e, cp, ex)
            fail += 1
    print(f"emoji: {ok} ok, {fail} failed -> {OUT}")


if __name__ == "__main__":
    main()
