"""Baseline of the speech pipeline on the recordings we actually made.

Why this exists: the user's complaint on 2026-10-26 was "short commands are
misheard", but nobody had a number.  This script replays every clip listed in
``metrics/asr_manifest.json`` through the current recogniser and reports

* CER (character error rate) against the sentence that was actually spoken,
* whether the (intent, target) routing survived the transcription,
* hallucination count on clips where nothing was said.

The manifest is plain JSON so the acceptance test ("10 commands, >= 9 right")
can be filled in later with ten fresh recordings without touching code.

Usage:
    python tools/asr_eval.py                     # use metrics/asr_manifest.json
    python tools/asr_eval.py --manifest x.json --verbose
"""
import argparse
import datetime
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from host.app.voice import eval as ve  # noqa: E402
from host.app.voice.asr import ChineseAsr, load_wav  # noqa: E402
from host.app.voice.intent import parse_intent  # noqa: E402

DEFAULT_MANIFEST = ROOT / "metrics" / "asr_manifest.json"


def _log(line):
    logs = ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / ("asr_eval_%s.log" % datetime.datetime.now().strftime("%Y%m%d"))
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("[%s] [INFO] [asr_eval] %s\n" % (stamp, line))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--no-evidence", action="store_true")
    args = ap.parse_args(argv)

    manifest_path = pathlib.Path(args.manifest)
    if not manifest_path.is_file():
        print("manifest not found:", manifest_path, file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("clips") or []
    print("manifest: %s (%d clips)" % (manifest_path.name, len(entries)))

    asr = ChineseAsr()
    rows = []
    started = time.time()
    for entry in entries:
        path = ROOT / entry["file"] if not pathlib.Path(entry["file"]).is_absolute() else pathlib.Path(entry["file"])
        if not path.is_file():
            print("  missing:", entry["file"], file=sys.stderr)
            continue
        audio, sr = load_wav(str(path))
        t0 = time.time()
        text = asr.transcribe(audio, sr)
        dt = time.time() - t0
        intent = parse_intent(text) if text else {}
        row = ve.score_entry(entry, text, intent)
        row["seconds"] = round(len(audio) / float(sr), 2)
        row["asr_ms"] = round(dt * 1000.0, 1)
        rows.append(row)
        tag = "ok " if (row["intent_ok"] is not False) else "MISS"
        print("  [%s] %-34s %4.1fs cer=%.2f -> '%s'"
              % (tag, path.name, row["seconds"], row["cer"], text))
        if args.verbose:
            print("        expected '%s' | intent %s/%s -> %s/%s"
                  % (row["expected"], row["intent_expected"], row["target_expected"],
                     row["intent_got"], row["target_got"]))

    summary = ve.summarize(rows, aggregate=manifest.get("aggregate") or [])
    summary["all_files_count"] = len(rows)
    summary["wall_s"] = round(time.time() - started, 1)
    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))

    if not args.no_evidence:
        out = ROOT / "metrics" / "evidence"
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        path = out / ("asr_eval_%s.json" % stamp)
        payload = {"manifest": manifest_path.name, "summary": summary, "rows": rows}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("evidence ->", path)
        _log("manifest=%s mean_cer=%s intent %s/%s hallucinations=%s"
             % (manifest_path.name, summary.get("mean_cer"),
                summary.get("intent_ok"), summary.get("intent_checked"),
                summary.get("hallucinations_on_silence")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
