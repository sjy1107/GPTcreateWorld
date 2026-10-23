"""Fixed-function console worker. The parent starts it with CREATE_NO_WINDOW."""
import json
import re
from pathlib import Path
import sys

if __name__ == '__main__':
    try:
        if len(sys.argv) != 4 or sys.argv[1] != '--engine-probe' or sys.argv[3] not in ('quick', 'full'):
            raise ValueError('后台程序请由拾影主窗口启动。')
        from video_demo.paths import data_root, prepare_portable
        prepare_portable()
        slot = sys.argv[2]
        if slot:
            from video_demo.engine_state import slot_path, root_path
            supplied = Path(slot)
            if supplied.is_symlink() or supplied.is_junction():
                raise ValueError('引擎验证位置不能是链接。')
            selected = supplied.resolve()
            engine = root_path().resolve()
            if (selected.name == 'package' and re.fullmatch(r'\.incoming-[a-f0-9]{32}', selected.parent.name)
                    and selected.parent.parent == engine and selected.is_dir()
                    and not selected.parent.is_symlink() and not selected.parent.is_junction()):
                expected = selected
            else:
                expected = slot_path(selected.name)
            if selected != expected.resolve():
                raise ValueError('引擎验证位置不正确。')
            sys.path.insert(0, str(selected))
        from video_demo.engine_probe import main
        main(sys.argv[3])
    except Exception as error:
        print(json.dumps({'error': str(error)[:1000]}, ensure_ascii=False))
        raise SystemExit(1)
