"""Frozen GUI entry; route explicit diagnostic tasks before importing Qt."""
import os
import sys

if __name__ == '__main__':
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w')
    if '--self-test' in sys.argv:
        from video_demo.portable_checks import main
        raise SystemExit(main())
    from video_demo.bootstrap import launch
    raise SystemExit(launch())
