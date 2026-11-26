"""8BitDo Retro Keyboard volume knob -> volume of the app that is playing.

Run hidden:   pythonw volume_knob.py   (or volume_knob.exe)
Stop:         volume_knob.py --stop
Health check: volume_knob.py --self-test [--out file]   (changes nothing)
Dev check:    volume_knob.py --test     (nudges app volumes by 0.1 % and restores them,
                                         sends two fake volume keys that cancel out)
When Brave starts it as its native-messaging host (first argument
chrome-extension://...), it runs the host instead (native_host package).
"""
import os
import sys

if getattr(sys, "frozen", False) is False:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    argv = sys.argv[1:]
    if argv and argv[0].startswith("chrome-extension://"):
        from native_host import volume_knob_host
        return volume_knob_host.run(argv)
    if argv and argv[0] in ("--install", "--uninstall"):
        import setup_knob
        return setup_knob.run(argv)
    from knob import app
    return app.main(argv)


if __name__ == "__main__":
    sys.exit(main())
