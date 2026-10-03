#!/usr/bin/env python3
"""
IronCAD Bridge entry point.

Opens the window by default, because the person commissioning a receiver is
standing at the machine. Runs headless with --no-ui, because a station that has
been commissioned should come back on its own after a power cut, under whatever
service manager the agency uses, with no window and nobody logged in.

Both paths run bridge.run_loop(). The window is a different face on the same
engine, not a second program.
"""

import sys


def _headless(reason=None):
    import bridge

    if sys.stdout is None:
        # A windowed build has no console. Without this a station that fell
        # back to headless would run correctly and report nothing at all,
        # which is worse than failing outright.
        emit = bridge.make_log_emit()

        def report(message):
            emit("error", message)
            sys.exit(1)
    else:
        emit = None
        report = None

    if reason:
        if emit:
            emit("warn", reason)
        else:
            print(reason, flush=True)

    bridge.main(emit=emit, report=report)


def main():
    if "--no-ui" in sys.argv or "--console" in sys.argv:
        _headless()
        return

    try:
        import ui
    except Exception as exc:  # noqa: BLE001 - no display, no Tk, anything
        # Falling through to the console is better than refusing to run: a
        # machine without a desktop session is exactly where headless is
        # wanted, and the operator still gets the engine.
        _headless(f"window unavailable ({exc}); running without it")
        return

    ui.run()


if __name__ == "__main__":
    main()
