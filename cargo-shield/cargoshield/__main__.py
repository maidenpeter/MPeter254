"""Command line: python -m cargoshield <command>

    seed     generate data, train the model, score everything, snapshot
    train    reset to the demo start, retrain, rescore and save as the new start
             (clears issued cover, reviews and live bookings since the seed)
    run      start the web app
    demo     seed, then run (one command for the whole demo)
    reset    put the database back to the demo start state
    report   print how well the engine finds the planted risks
    doctor   check that everything the demo needs is installed
    test     run the test suite
"""

from __future__ import annotations

import argparse
import importlib
import socket
import subprocess
import sys


def _lan_ip() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packets are sent
        ip = s.getsockname()[0]
        s.close()
        return ip if not ip.startswith("127.") else None
    except OSError:
        return None


def _not_seeded() -> bool:
    from . import db

    if db.is_seeded():
        return False
    print("No demo database yet. Run `make seed` (or `python -m cargoshield seed`) first.")
    return True


def cmd_seed(args) -> int:
    from . import seed

    seed.run(args.bookings, args.shippers, args.seed)
    return 0


def cmd_train(args) -> int:
    """Retrain and rescore from the demo starting state, then make that the new
    starting state, so Reset keeps the tuned rules. A running app picks up the
    new model and rules on its next request."""
    from . import anomaly, config, db, evaluation, risk, seed

    if _not_seeded():
        return 1
    if config.snapshot_path().exists():
        db.restore()
    conn = db.connect()
    model = anomaly.train(conn)
    anomaly.save(model)
    # New rules can send already-departed flights to review: give them the decision a
    # reviewer would have made at the time, scoring until the two agree (as the seed does).
    seed.settle_reviews(conn, model, db.utcnow())
    db.snapshot(conn)
    print(evaluation.format_report(evaluation.engine_report(conn)))
    print("Saved as the new demo starting state (what Reset restores).")
    conn.close()
    return 0


def cmd_run(args) -> int:
    import uvicorn

    from . import config, db

    srv = config.settings()["server"]
    host = args.host or srv["host"]
    port = args.port or int(srv["port"])
    if not db.is_seeded():
        print("No demo database yet, seeding first...")
        cmd_seed(argparse.Namespace(bookings=None, shippers=None, seed=None))
    print(f"\nCargoShield is starting on http://localhost:{port}")
    if host in ("0.0.0.0", "::"):
        ip = _lan_ip()
        if ip:
            print(f"On your network (for scanning QR codes with a phone): http://{ip}:{port}")
    else:
        print("Only this laptop can reach it. Use HOST=0.0.0.0 to let a phone on the same Wi-Fi scan QR codes.")
    print("Press Ctrl+C to stop.\n")
    uvicorn.run("cargoshield.app:app", host=host, port=port, log_level="warning")
    return 0


def cmd_demo(args) -> int:
    cmd_seed(argparse.Namespace(bookings=None, shippers=None, seed=None))
    return cmd_run(args)


def cmd_reset(args) -> int:
    from . import config, db

    try:
        db.restore()
    except FileNotFoundError:
        print("No snapshot found, seeding a fresh demo instead.")
        return cmd_seed(argparse.Namespace(bookings=None, shippers=None, seed=None))
    print(f"Database restored to the demo start state ({config.db_path()}).")
    return 0


def cmd_report(args) -> int:
    from . import db, evaluation

    if _not_seeded():
        return 1
    conn = db.connect()
    print(evaluation.format_report(evaluation.engine_report(conn)))
    conn.close()
    return 0


def cmd_doctor(args) -> int:
    ok = True

    def line(good: bool, label: str, hint: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"  [{'ok' if good else '!!'}] {label}" + (f"  ->  {hint}" if hint and not good else ""))

    print("CargoShield pre-flight check")
    line(sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}", "Python 3.11 or newer is required")
    for mod, pip_name in [("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("multipart", "python-multipart"),
                          ("yaml", "pyyaml"), ("sklearn", "scikit-learn"), ("faker", "faker"),
                          ("qrcode", "qrcode"), ("PIL", "pillow"), ("pytesseract", "pytesseract")]:
        try:
            importlib.import_module(mod)
            line(True, f"{pip_name} installed")
        except ImportError:
            line(False, f"{pip_name} missing", "pip install -r requirements.txt")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo("Africa/Nairobi")
        line(True, "time zone data available")
    except Exception:
        line(False, "time zone data missing", "pip install tzdata")
    if not ok:
        print("Install the missing packages, then run doctor again.")
        return 1
    from . import config, db, ocr, seed

    tess = ocr.tesseract_available()
    print(f"  [{'ok' if tess else '--'}] Tesseract OCR " + ("found" if tess else
          "not found. Photos will ask for the number instead. Install: brew install tesseract "
          "(Mac), sudo apt install tesseract-ocr (Linux), or the UB Mannheim installer (Windows)."))
    qr = ocr.qr_available()
    print(f"  [{'ok' if qr else '--'}] QR decoding " + ("available" if qr else
          "unavailable (pip install zxing-cpp). The certificate can still be checked by its AWB number."))
    if seed.routes_are_placeholder():
        print("  [--] data/routes.csv is still the placeholder schedule. Refresh it from the airline's "
              "public schedule before a real pitch, or say 'placeholder schedule' when presenting.")
    ready = db.is_seeded() and config.model_path().exists() and config.snapshot_path().exists()
    print(f"  [{'ok' if ready else '--'}] Demo data " + ("seeded" if ready else
          "not generated yet. `make demo` (or `make seed`) creates it."))
    if not ok:
        print("Fix the items marked !! before the demo.")
    elif ready:
        print("Ready for the demo.")
    else:
        print("Software is ready. Run `make demo` to generate the data and start the app.")
    return 0 if ok else 1


def cmd_test(args) -> int:
    from . import config

    return subprocess.call([sys.executable, "-m", "pytest", *args.pytest_args], cwd=str(config.PROJECT_ROOT))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cargoshield", description="CargoShield demo")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("seed", help="generate data, train, score, snapshot")
    p.add_argument("--bookings", type=int)
    p.add_argument("--shippers", type=int)
    p.add_argument("--seed", type=int)
    p.set_defaults(fn=cmd_seed)
    sub.add_parser("train", help="reset to the demo start, retrain, rescore, save as the new start").set_defaults(fn=cmd_train)
    for name, fn in (("run", cmd_run), ("demo", cmd_demo)):
        p = sub.add_parser(name, help="start the web app" if name == "run" else "seed, then start the web app")
        p.add_argument("--host")
        p.add_argument("--port", type=int)
        p.set_defaults(fn=fn)
    sub.add_parser("reset", help="restore the demo start state").set_defaults(fn=cmd_reset)
    sub.add_parser("report", help="engine detection and false-positive report").set_defaults(fn=cmd_report)
    sub.add_parser("doctor", help="pre-flight check").set_defaults(fn=cmd_doctor)
    p = sub.add_parser("test", help="run the tests")
    p.add_argument("pytest_args", nargs="*")
    p.set_defaults(fn=cmd_test)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
