"""Observe the production main/Qt/analyzer path; isolate all persisted output.

Offscreen runs verify Qt rendering only, never physical screen visibility.
No detector, alert, reader, or flight implementation is replaced.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from replay_tracker import ROOT, load_tracker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=48)
    parser.add_argument('--device', default='cpu')
    parser.add_argument('--offscreen', action='store_true')
    parser.add_argument('sources', nargs='+', type=Path)
    args = parser.parse_args()
    if args.offscreen:
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    sources = [str(p.resolve(strict=True)) for p in args.sources]
    output = args.output.resolve()
    if output.exists():
        parser.error('Use a fresh output directory')
    output.mkdir(parents=True)
    (output / 'models').symlink_to(ROOT / 'models', target_is_directory=True)
    import cv2
    app = load_tracker()
    source_hash = hashlib.sha256((ROOT / 'legacy/drone_tracker.py').read_bytes()).hexdigest()
    app.ROOT = output
    rows, attempts, analyses = [], [], []
    inference_frames = {}
    start = time.monotonic()
    original_emit = app.EventManager.emit
    original_process = app.Analyzer.process
    original_draw = app.Dashboard.draw

    def emit(manager, channel, kind, *a, **kw):
        before = set(output.glob('data/events/**/event.json'))
        result = original_emit(manager, channel, kind, *a, **kw)
        audit = dict(t=time.monotonic()-start, channel=channel.number,
                     kind=kind, accepted=bool(result))
        if result and channel.number in inference_frames:
            for meta_path in set(output.glob('data/events/**/event.json'))-before:
                meta = json.loads(meta_path.read_text())
                frame = inference_frames[channel.number]
                target = meta_path.parent / 'inference_frame.jpg'
                cv2.imwrite(str(target), frame)
                audit['inference_frame'] = str(target)
                audit['snapshot_exactly_matches_inference_frame'] = (
                    target.read_bytes() == Path(meta['snapshot']).read_bytes())
        attempts.append(audit)
        return result

    def process(analyzer, channel, frame, *a, **kw):
        # Production signature: channel, general_model, pose, ppe, fire_model, frame.
        inference_frames[channel.number] = a[-1]
        t = time.monotonic()
        result = original_process(analyzer, channel, frame, *a, **kw)
        # Event snapshots originate from pending, which can advance during inference.
        analyses.append(dict(t=t-start, channel=channel.number,
                             processing_ms=(time.monotonic()-t)*1000,
                             objects={str(k): v for k, v in channel.objects.items()}))
        return result

    last_sample = [-1]
    def draw(dashboard):
        elapsed = time.monotonic()-start
        selected = min(int(elapsed // 12) % len(sources), len(sources)-1)
        dashboard.select(selected)
        result = original_draw(dashboard)
        second = int(elapsed)
        if second != last_sample[0]:
            last_sample[0] = second
            view = dashboard.main_view
            row = dict(t=elapsed, selected=selected+1,
                       risk=dashboard.current_risk, analyzer_error=dashboard.analyzer.error,
                       qt_platform=dashboard.dialog_app.platformName(),
                       channels=[dict(number=c.number, status=c.status,
                           tracked_objects=len(c.objects), fire_active=time.time()<c.fire_until,
                           smoke_active=time.time()<c.smoke_until) for c in dashboard.channels if c.source])
            rows.append(row)
            # Save actual QWidget pixels, including the right RISK panel.
            view.window.grab().save(str(output / f'window_{second:03d}.png'))
            print(json.dumps(row, default=str), flush=True)
        if elapsed >= args.seconds:
            dashboard.key_queue.append(ord('q'))
        return result

    app.EventManager.emit = emit
    app.Analyzer.process = process
    app.Dashboard.draw = draw
    sys.argv = ['drone_tracker.py', *sources, '--device', args.device, '--flight-adapter', 'SIM']
    try:
        app.main()
    finally:
        events = []
        for path in sorted(output.glob('data/events/**/event.json')):
            meta = json.loads(path.read_text())
            snapshot = Path(meta.get('snapshot', ''))
            events.append(dict(metadata=str(path), event=meta,
                               snapshot_decodes=bool(snapshot.is_file() and cv2.imread(str(snapshot)) is not None)))
        report = dict(native_gui_verified=False,
                      mode='offscreen' if args.offscreen else 'native_requires_visual_review',
                      tracker_sha256=source_hash,
                      tracker_unchanged=source_hash == hashlib.sha256((ROOT/'legacy/drone_tracker.py').read_bytes()).hexdigest(),
                      sources=sources, rows=rows, analyses=analyses,
                      event_attempts=attempts, events=events,
                      limitations=['No ground truth accuracy score',
                                   'Snapshot/inference target alignment requires visual review',
                                   'Alert release/reoccurrence requires review of timeline and screenshots',
                                   'SIM adapter; no PX4 regression test'])
        (output/'report.json').write_text(json.dumps(report, indent=2, default=str))


if __name__ == '__main__':
    main()
