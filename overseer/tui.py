"""Friendly full-screen dialogs over SSH (whiptail), with a plain-text fallback. Stdlib only.

Set OVERSEER_PLAIN=1 to force plain prompts (e.g. dumb terminals, tests).
"""
import getpass, os, shutil, subprocess, sys

TITLE = "Lab Overseer"


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
            v = input(f"choice [{d}]: ").strip() or d
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
        v = input("Enter to keep, or type numbers to use instead (e.g. 1,3,4): ").strip()
        if not v:
            return [k for k, _, c in items if c]
        pick = {int(x) for x in v.replace(" ", "").split(",") if x.isdigit()}
        return [k for i, (k, _, _) in enumerate(items, 1) if i in pick]

    def text(self, title, body):
        print(f"\n== {title}\n{body}\n")
        input("[Enter] ")


class Whiptail(Plain):
    gui = True

    def _run(self, args, height=None, width=76, pre=()):
        rows, cols = shutil.get_terminal_size((80, 24))
        h = min(height or 20, rows - 2)
        w = min(cols - 4, width)
        r = subprocess.run(["whiptail", "--title", TITLE, *pre, args[0], args[1], str(h), str(w), *args[2:]],
                           stderr=subprocess.PIPE, text=True)
        if r.returncode == 255:        # Esc
            raise KeyboardInterrupt
        return r.returncode, r.stderr.strip()

    def msg(self, text, title=TITLE):
        self._run(["--msgbox", text], height=min(22, 8 + text.count("\n") + len(text) // 70))

    def info(self, text):
        # non-blocking status line; whiptail infobox clears on next screen
        subprocess.run(["whiptail", "--title", TITLE, "--infobox", text, "8", "70"])

    def ask(self, q, default="", secret=False):
        rc, v = self._run(["--passwordbox" if secret else "--inputbox", q, *([] if secret else [default])],
                          height=10 + q.count("\n"))
        if rc == 1:
            raise KeyboardInterrupt
        return v or ("" if secret else default)

    def yes(self, q, default=True):
        rc, _ = self._run(["--yesno", q], height=10 + q.count("\n") + len(q) // 70, pre=() if default else ("--defaultno",))
        return rc == 0

    def menu(self, q, items, default=None):
        args = ["--menu", q, str(len(items))]
        for k, l in items:
            args += [str(k), l]
        pre = ("--default-item", str(default)) if default is not None else ()
        rc, v = self._run(args, height=min(22, 9 + len(items) + q.count("\n")), pre=pre)
        if rc != 0:
            raise KeyboardInterrupt
        return next(k for k, _ in items if str(k) == v)

    def checklist(self, q, items):
        args = ["--checklist", q, str(min(len(items), 14))]
        for k, l, c in items:
            args += [str(k), l, "ON" if c else "OFF"]
        rc, v = self._run(args, height=min(24, 9 + len(items) + q.count("\n")))
        if rc != 0:
            raise KeyboardInterrupt
        chosen = {x.strip('"') for x in v.split()}
        return [k for k, _, _ in items if str(k) in chosen]

    def text(self, title, body):
        import tempfile
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt") as f:
            f.write(body)
        try:
            rows, cols = shutil.get_terminal_size((80, 24))
            subprocess.run(["whiptail", "--title", title, "--scrolltext", "--textbox", f.name, str(rows - 2), str(min(cols - 2, 110))])
        finally:
            os.unlink(f.name)


def get_ui():
    if os.environ.get("OVERSEER_PLAIN") or not sys.stdin.isatty() or not shutil.which("whiptail"):
        return Plain()
    return Whiptail()
