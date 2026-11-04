"""Command line: python -m cargoshield <command>

    seed     generate data, train the model, score everything, snapshot
    train    retrain the anomaly model and rescore every booking
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

from . import config


def _lan_ip() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packets are sent
        ip = s.getsockname()[0]
        s.close()
        return ip if not ip.startswith("127.") else None
    except OSError:
        return None


def cmd_seed(args) -> int:
    from . import seed

    seed.run(args.bookings, args.shippers, args.seed)
    return 0


def cmd_train(args) -> int:
    from . import anomaly, db, evaluation, risk

    conn = db.connect()
    model = anomaly.train(conn)
    anomaly.save(model)
    risk.score_all(conn, model)
    print(evaluation.format_report(evaluation.engine_report(conn)))
    conn.close()
    return 0


def cmd_run(args) -> int:
    import uvicorn

    srv = config.settings()["server"]
    host = args.host or srv["host"]
    port = args.port or int(srv["port"])
    if not config.db_path().exists():
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
    from . import db

    try:
        db.restore()
    except FileNotFoundError:
        print("No snapshot found, seeding a fresh demo instead.")
        return cmd_seed(argparse.Namespace(bookings=None, shippers=None, seed=None))
    print(f"Database restored to the demo start state ({config.db_path()}).")
    return 0


def cmd_report(args) -> int:
    from . import db, evaluation

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
    from . import ocr

    tess = ocr.tesseract_available()
    print(f"  [{'ok' if tess else '--'}] Tesseract OCR " + ("found" if tess else
          "not found. Photos will ask for the number instead. Install: brew install tesseract "
          "(Mac), sudo apt install tesseract-ocr (Linux), or the UB Mannheim installer (Windows)."))
    qr = ocr.qr_available()
    print(f"  [{'ok' if qr else '--'}] QR decoding " + ("available" if qr else
          "unavailable (pip install zxing-cpp). The certificate can still be checked by its AWB number."))
    line(config.db_path().exists(), "Demo database present", "run: make seed")
    line(config.model_path().exists(), "Anomaly model present", "run: make seed")
    line(config.snapshot_path().exists(), "Reset snapshot present", "run: make seed")
    print("Ready for the demo." if ok else "Fix the items marked !! before the demo.")
    return 0 if ok else 1


def cmd_test(args) -> int:
    return subprocess.call([sys.executable, "-m", "pytest", *args.pytest_args], cwd=str(config.PROJECT_ROOT))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cargoshield", description="CargoShield demo")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("seed", help="generate data, train, score, snapshot")
    p.add_argument("--bookings", type=int)
    p.add_argument("--shippers", type=int)
    p.add_argument("--seed", type=int)
    p.set_defaults(fn=cmd_seed)
    sub.add_parser("train", help="retrain the anomaly model and rescore").set_defaults(fn=cmd_train)
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
