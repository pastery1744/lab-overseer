"""Friendly full-screen dialogs over SSH (whiptail), with a plain-text fallback. Stdlib only.

Set OVERSEER_PLAIN=1 to force plain prompts (e.g. dumb terminals, tests).
"""
import getpass, os, shutil, subprocess, sys

TITLE = "Lab Overseer"
# whiptail can only draw arrows/dashes/bullets/emoji under a UTF-8 locale; installers often run with LC_ALL=C
_ENV = dict(os.environ, LANG="C.UTF-8", LC_ALL="C.UTF-8")


class Back(Exception):
    """User pressed Back — the wizard steps to the previous screen."""


class Plain:
    gui = False

    def msg(self, text, title=TITLE):
        print(f"\n== {title}\n{text}\n")
        input("[Enter] ")

    def info(self, text):
        print(text)

    def ask(self, q, default="", secret=False):
        p = f"{q}" + (f" [{default}]" if default and not secret else "") + ": "
        v = (getpass.getpass(p) if secret else input(p)).strip()
        if v.lower() in ("<", "back"):
            raise Back
        return v or default

    def yes(self, q, default=True):
        v = input(f"{q} ({'Y/n' if default else 'y/N'}): ").strip().lower()
        return default if not v else v.startswith("y")

    def menu(self, q, items, default=None):
        """items: [(key, label)] -> key"""
        print(f"\n{q}")
        for i, (k, l) in enumerate(items, 1):
            print(f"  {i}) {l}")
        d = next((str(i) for i, (k, _) in enumerate(items, 1) if k == default), "1")
        while True:
            v = input(f"choice [{d}] (or 'back'): ").strip() or d
            if v.lower() in ("<", "back"):
                raise Back
            if v.isdigit() and 1 <= int(v) <= len(items):
                return items[int(v) - 1][0]
            for k, _ in items:
                if v.lower() == str(k).lower():
                    return k

    def checklist(self, q, items):
        """items: [(key, label, checked)] -> [keys]"""
        print(f"\n{q}")
        for i, (k, l, c) in enumerate(items, 1):
            print(f"  [{'x' if c else ' '}] {i}) {l}")
        v = input("Enter to keep, numbers to use instead (e.g. 1,3,4), or 'back': ").strip()
        if v.lower() in ("<", "back"):
            raise Back
        if not v:
            return [k for k, _, c in items if c]
        pick = {int(x) for x in v.replace(" ", "").split(",") if x.isdigit()}
        return [k for i, (k, _, _) in enumerate(items, 1) if i in pick]

    def text(self, title, body):
        print(f"\n== {title}\n{body}\n")
        input("[Enter] ")


def _text_lines(q, width):
    """How many rows a prompt takes once whiptail wraps it inside a box `width` wide."""
    inner = max(20, width - 4)
    return sum(max(1, -(-len(line) // inner)) for line in q.split("\n"))


def _box_width(want):
    cols, _ = shutil.get_terminal_size((80, 24))
    return max(40, min(cols - 4, want))


class _Redo(Exception):
    pass


class Whiptail(Plain):
    gui = True

    def _run(self, args, height=None, width=None, pre=()):
        cols, rows = shutil.get_terminal_size((80, 24))
        h = min(height or 20, rows - 2)
        w = max(40, min(cols - 4, width or 78))
        r = subprocess.run(["whiptail", "--title", TITLE, *pre, args[0], args[1], str(h), str(w), *args[2:]],
                           stderr=subprocess.PIPE, text=True, env=_ENV)
        if r.returncode == 255:        # Esc: don't just die — ask
            self._escape()
        return r.returncode, r.stderr.strip()

    def _escape(self):
        r = subprocess.run(["whiptail", "--title", TITLE, "--nocancel", "--menu", "Paused. What would you like to do?", "12", "60", "3",
                            "back", "Go back one step", "stay", "Keep going (redo this screen)", "quit", "Quit setup (nothing is saved)"],
                           stderr=subprocess.PIPE, text=True, env=_ENV)
        choice = r.stderr.strip()
        if choice == "quit":
            raise KeyboardInterrupt
        if choice == "back":
            raise Back
        raise _Redo

    def _call(self, fn, *a, **kw):
        while True:
            try:
                return fn(*a, **kw)
            except _Redo:
                continue

    def msg(self, text, title=TITLE):
        w = _box_width(max(50, min(78, max(len(x) for x in text.split("\n")) + 6)))
        self._call(self._run, ["--msgbox", text], height=7 + _text_lines(text, w), width=w)

    def info(self, text):
        # non-blocking status line; whiptail infobox clears on next screen
        subprocess.run(["whiptail", "--title", TITLE, "--infobox", text, "8", "70"], env=_ENV)

    def ask(self, q, default="", secret=False):
        rc, v = self._call(self._run, ["--passwordbox" if secret else "--inputbox", q, *([] if secret else [default])],
                           height=9 + _text_lines(q, 78), pre=("--cancel-button", "Back"))
        if rc == 1:
            raise Back
        return v or ("" if secret else default)

    def yes(self, q, default=True):
        w = _box_width(max(50, min(78, max(len(x) for x in q.split("\n")) + 6)))
        rc, _ = self._call(self._run, ["--yesno", q], height=7 + _text_lines(q, w), width=w, pre=() if default else ("--defaultno",))
        return rc == 0

    def menu(self, q, items, default=None):
        args = ["--menu", q, str(len(items))]
        for k, l in items:
            args += [str(k), l]
        pre = ("--notags", "--cancel-button", "Back", *(("--default-item", str(default)) if default is not None else ()))
        w = _box_width(max(56, max((len(l) for _, l in items), default=20) + 12, max(len(x) for x in q.split("\n")) + 6))
        rc, v = self._call(self._run, args, height=7 + len(items) + _text_lines(q, w), width=w, pre=pre)
        if rc != 0:
            raise Back
        return next(k for k, _ in items if str(k) == v)

    def checklist(self, q, items):
        cols, rows = shutil.get_terminal_size((80, 24))
        w = _box_width(max(56, max((len(l) for _, l, _ in items), default=20) + 16, max(len(x) for x in q.split("\n")) + 6))
        tl = _text_lines(q, w)
        list_h = max(3, min(len(items), rows - 8 - tl))
        args = ["--checklist", q, str(list_h)]
        for k, l, c in items:
            args += [str(k), l, "ON" if c else "OFF"]
        rc, v = self._call(self._run, args, height=list_h + 6 + tl, width=w,
                           pre=("--notags", "--cancel-button", "Back"))
        if rc != 0:
            raise Back
        chosen = {x.strip('"') for x in v.split()}
        return [k for k, _, _ in items if str(k) in chosen]

    def text(self, title, body):
        import tempfile
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt") as f:
            f.write(body)
        try:
            cols, rows = shutil.get_terminal_size((80, 24))
            subprocess.run(["whiptail", "--title", title, "--scrolltext", "--textbox", f.name, str(rows - 2), str(min(cols - 2, 110))], env=_ENV)
        finally:
            os.unlink(f.name)


def get_ui():
    if os.environ.get("OVERSEER_PLAIN") or not sys.stdin.isatty() or not shutil.which("whiptail"):
        return Plain()
    return Whiptail()
