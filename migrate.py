#!/usr/bin/env python3
"""WeChat saved stickers -> Feishu personal stickers on macOS and Windows.

Reads local WeChat favorites and uses optional desktop UI automation. No Feishu API.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

HOME = Path.home()
ROOT = HOME / 'WeChat-Feishu-Stickers'
CONFIG = ROOT / 'config.json'
STATE = ROOT / 'state.json'
EXPORT = ROOT / 'exported'
RAW_EXPORT = ROOT / '.wxemoticon-cache'
GROUP_MANIFEST = ROOT / 'group_manifest.json'
GROUP_SIZE = 50
EXTENSIONS = {'.gif', '.png', '.jpg', '.jpeg', '.webp', '.bmp', '.apng'}


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def require_desktop_platform():
    if sys.platform not in ('darwin', 'win32'):
        raise RuntimeError('微信导出与飞书自动化仅支持 macOS 和 Windows；其他系统可使用 --group-only。')


def run_export(wechat_data_dir=None, wxid=None, key_file=None):
    require_desktop_platform()
    from wechat_source import export_favorites
    RAW_EXPORT.mkdir(parents=True, exist_ok=True)
    images = export_favorites(RAW_EXPORT, ROOT / 'download_manifest.json',
                              data_dir=wechat_data_dir, wxid=wxid, key_file=key_file)
    save_json(ROOT / 'source_order.json', [p.name for p in images])
    return images


def is_image(path):
    return path.is_file() and path.suffix.lower() in EXTENSIONS


def move_old_flat_exports():
    """Keep old completed downloads while switching to numbered folders.

    Keep the historical cache location so existing grouped downloads remain
    usable after switching to the independent exporter.
    """
    RAW_EXPORT.mkdir(parents=True, exist_ok=True)
    EXPORT.mkdir(parents=True, exist_ok=True)
    moved = 0
    for source in sorted(EXPORT.iterdir(), key=lambda p: p.name):
        if not is_image(source):
            continue
        dest = RAW_EXPORT / source.name
        if dest.exists():
            if fingerprint(dest) == fingerprint(source):
                source.unlink()
                moved += 1
                continue
            # Preserve both files when old and new downloads share a name.
            digest = fingerprint(source)[:12]
            dest = RAW_EXPORT / f'{source.stem}__{digest}{source.suffix}'
            if dest.exists():
                if fingerprint(dest) != fingerprint(source):
                    raise RuntimeError(f'缓存文件名冲突，请手动检查：{dest}')
                source.unlink()
                moved += 1
                continue
        source.rename(dest)
        moved += 1
    return moved


def group_exports():
    """Create stable 1/, 2/, ... directories, maximum 50 images each.

    Existing exports and download cache are never overwritten. Numbered
    folders use hard links where possible, so no duplicate image storage is
    needed on the same filesystem; copy is a safe fallback.
    """
    moved = move_old_flat_exports()
    manifest = load_json(GROUP_MANIFEST, {'version': 1, 'assignments': {}})
    if manifest.get('version') != 1 or not isinstance(manifest.get('assignments'), dict):
        raise RuntimeError(f'分组清单格式无效：{GROUP_MANIFEST}')
    assignments = manifest['assignments']

    # Recover an existing grouped layout if the manifest was deleted.
    for group_dir in sorted(EXPORT.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else -1):
        if not group_dir.is_dir() or not group_dir.name.isdigit() or int(group_dir.name) < 1:
            continue
        for existing in group_dir.iterdir():
            if is_image(existing) and existing.name not in assignments:
                assignments[existing.name] = {'group': int(group_dir.name), 'name': existing.name}

    # Ensure reserved slots also account for files that are temporarily missing
    # from the cache, so newly exported files never reshuffle existing groups.
    counts = {}
    for info in assignments.values():
        group = info.get('group')
        if not isinstance(group, int) or group < 1:
            raise RuntimeError('分组清单含无效组号')
        counts[group] = counts.get(group, 0) + 1
    if any(n > GROUP_SIZE for n in counts.values()):
        raise RuntimeError(f'已有分组超过 {GROUP_SIZE} 张，请检查 {GROUP_MANIFEST}')

    cached = {p.name: p for p in RAW_EXPORT.iterdir() if is_image(p)}
    ordered = load_json(ROOT / 'source_order.json', [])
    raw_images = [cached.pop(name) for name in dict.fromkeys(ordered) if name in cached]
    raw_images.extend(sorted(cached.values(), key=lambda p: p.name))
    added = 0
    repaired = 0
    for source in raw_images:
        info = assignments.get(source.name)
        if info is None:
            group = max(counts, default=1)
            if counts.get(group, 0) >= GROUP_SIZE:
                group += 1
            info = {'group': group, 'name': source.name}
            counts[group] = counts.get(group, 0) + 1
            assignments[source.name] = info
            added += 1
        dest_dir = EXPORT / str(info['group'])
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / info['name']
        if not dest.exists():
            try:
                os.link(source, dest)  # same inode, no second full-size copy
            except OSError:
                # A copy is needed if hard links are unavailable.
                temporary = dest.with_name(dest.name + '.copying')
                try:
                    shutil.copy2(source, temporary)
                    temporary.replace(dest)
                finally:
                    temporary.unlink(missing_ok=True)
            repaired += 1
        elif not dest.samefile(source) and fingerprint(dest) != fingerprint(source):
            raise RuntimeError(f'已分组文件内容与缓存不一致，请检查：{dest}')
        # Save after each successful item, allowing interruption / resume.
        save_json(GROUP_MANIFEST, manifest)

    grouped = all_images()
    folder_counts = {}
    for path in grouped:
        folder_counts[path.parent.name] = folder_counts.get(path.parent.name, 0) + 1
    print(f'分组完成：{len(grouped)} 张，{len(folder_counts)} 个文件夹，每组最多 {GROUP_SIZE} 张。')
    if moved:
        print(f'已将旧版目录中的 {moved} 张原始图片迁移到下载缓存，无需重新下载。')
    if added or repaired:
        print(f'新分配 {added} 张，创建/修复分组文件 {repaired} 张。')
    print(f'分组路径：{EXPORT}')
    if folder_counts:
        details = '，'.join(f'{name}: {n} 张' for name, n in sorted(folder_counts.items(), key=lambda x: int(x[0])))
        print('各组数量：' + details)
    return grouped


def all_images():
    """Read only numbered output folders, never count raw cache twice."""
    if not EXPORT.exists():
        return []
    images = []
    for group_dir in EXPORT.iterdir():
        if group_dir.is_dir() and group_dir.name.isdigit() and int(group_dir.name) > 0:
            images.extend(p for p in group_dir.iterdir() if is_image(p))
    return sorted(images, key=lambda p: (int(p.parent.name), p.name))


def fingerprint(path):
    h = hashlib.sha256()
    with path.open('rb') as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def is_feishu_window(window):
    import ctypes
    from ctypes import wintypes
    import psutil

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    get_pid = user32.GetWindowThreadProcessId
    get_pid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    get_pid.restype = wintypes.DWORD
    pid = wintypes.DWORD()
    if not get_pid(window._hWnd, ctypes.byref(pid)) or not pid.value:
        return False
    try:
        return psutil.Process(pid.value).name().casefold() in ('feishu.exe', 'lark.exe')
    except psutil.Error:
        return False


def activate_feishu(app_name):
    require_desktop_platform()
    if sys.platform == 'win32':
        try:
            import pygetwindow as gw
            import psutil  # Verify window ownership before clicking or pasting.
        except ImportError as exc:
            raise RuntimeError('缺少 Windows 窗口依赖，请用当前 Python 执行 -m pip install -r requirements.txt。') from exc
        names = ('飞书', 'Feishu') if app_name.casefold() in ('飞书', 'feishu') else (app_name,)
        windows = [w for w in gw.getAllWindows() if w.title and any(n.casefold() in w.title.casefold() for n in names) and is_feishu_window(w)]
        exact = [w for w in windows if any(w.title.casefold() == n.casefold() for n in names)]
        windows = exact or windows
        if len(windows) != 1:
            raise RuntimeError('无法唯一定位飞书窗口，请打开飞书主窗口、关闭多余同名窗口，或用 --calibrate --app-name 指定窗口标题。')
        window = windows[0]
        if window.isMinimized:
            window.restore()
            time.sleep(.3)
        try:
            window.activate()
        except Exception as exc:
            if not window.isActive:
                raise RuntimeError('Windows 未允许激活飞书，请先手动切换到飞书，再重新运行。') from exc
        time.sleep(.3)
        if not window.isActive:
            raise RuntimeError('飞书未成为前台窗口，已停止自动化，请手动切换到飞书后重试。')
        return
    # App name is escaped as an AppleScript string rather than interpolated raw.
    escaped = app_name.replace('\\', '\\\\').replace('"', '\\"')
    code = f'tell application "{escaped}" to activate'
    subprocess.run(['osascript', '-e', code], check=True, capture_output=True, text=True)


def set_clipboard(text):
    require_desktop_platform()
    if sys.platform == 'win32':
        try:
            import pyperclip
        except ImportError as exc:
            raise RuntimeError('缺少 Windows 剪贴板依赖，请用当前 Python 执行 -m pip install -r requirements.txt。') from exc
        pyperclip.copy(text)
        return
    # osascript argv keeps filenames out of AppleScript source.
    script = 'on run argv\nset the clipboard to item 1 of argv\nend run'
    subprocess.run(['osascript', '-e', script, text], check=True, capture_output=True, text=True)


def ui_lib():
    require_desktop_platform()
    if sys.platform == 'win32':
        # Match PyAutoGUI's coordinate system before recording scaled positions.
        import ctypes
        ctypes.windll.user32.SetProcessDPIAware()
    try:
        import pyautogui as pg
    except ImportError as exc:
        raise RuntimeError('缺少依赖，请用当前 Python 执行 -m pip install -r requirements.txt。') from exc
    pg.FAILSAFE = True
    pg.PAUSE = .25
    return pg


def calibrate(app_name):
    pg = ui_lib()
    print('\n首次校准：请在飞书打开任意聊天，依次点 表情 → 添加的单个表情。')
    print('然后把鼠标移到「+ 添加表情」按钮正中间（不要点击），回终端按回车。')
    input('准备好后按回车，3 秒后记录鼠标位置...')
    time.sleep(3)
    position = pg.position()
    cfg = {'platform': sys.platform, 'app_name': app_name, 'add_button': [position.x, position.y], 'picker_delay': 1.2, 'upload_delay': 1.8}
    save_json(CONFIG, cfg)
    print(f'已记录添加按钮位置：({position.x}, {position.y})，配置保存在 {CONFIG}')
    return cfg


def require_windows_picker():
    import ctypes
    from ctypes import wintypes
    from types import SimpleNamespace

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    handle = user32.GetForegroundWindow()
    name = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(handle, name, len(name))
    if name.value != '#32770' or not is_feishu_window(SimpleNamespace(_hWnd=handle)):
        raise RuntimeError('前台不是飞书的标准文件选择器，已停止输入。请手动添加本批图片。')


def choose_files(pg, paths, cfg):
    require_desktop_platform()
    if not 1 <= len(paths) <= GROUP_SIZE:
        raise RuntimeError('每批必须包含 1 至 50 张图片。')
    paths = [p.resolve() for p in paths]
    if len({p.parent for p in paths}) != 1 or any(not p.is_file() for p in paths):
        raise RuntimeError('同一批必须是同一分组目录中的现有图片。')
    if sys.platform != 'win32' and len(paths) > 1:
        raise RuntimeError('macOS 暂仅支持逐张自动添加；整批添加请使用 --manual-import。')
    x, y = cfg['add_button']
    pg.click(x, y)
    time.sleep(cfg['picker_delay'])
    if sys.platform == 'win32':
        require_windows_picker()
        # Navigate explicitly, then select only the files in this batch.
        pg.hotkey('alt', 'd')
        pg.hotkey('ctrl', 'a')
        set_clipboard(str(paths[0].parent))
        pg.hotkey('ctrl', 'v')
        pg.press('enter')
        time.sleep(.5)
        require_windows_picker()
        pg.hotkey('alt', 'n')
        pg.hotkey('ctrl', 'a')
        set_clipboard(' '.join('"' + p.name + '"' for p in paths))
        pg.hotkey('ctrl', 'v')
        time.sleep(.4)
        pg.press('enter')
    else:
        pg.hotkey('command', 'shift', 'g')
        time.sleep(.45)
        set_clipboard(str(paths[0]))
        pg.hotkey('command', 'v')
        time.sleep(.4)
        pg.press('return')
        time.sleep(.65)
        pg.press('return')
    time.sleep(cfg['upload_delay'])


def choose_file(pg, path, cfg):
    choose_files(pg, [path], cfg)


def import_batches(pending, batch_size):
    batches = []
    for item in pending:
        if not batches or len(batches[-1]) >= batch_size or batches[-1][0][0].parent != item[0].parent:
            batches.append([])
        batches[-1].append(item)
    return batches


def open_folder(path):
    if sys.platform == 'win32':
        os.startfile(str(path))
    else:
        subprocess.run(['open', str(path)], check=True)


def confirmed_indices(answer, count):
    answer = answer.strip().casefold()
    if answer in ('y', 'yes', '全部'):
        return set(range(count))
    if answer in ('', 'n', 'no', '无'):
        return set()
    try:
        indices = {int(token.strip()) - 1 for token in answer.split(',')}
    except ValueError as exc:
        raise ValueError('请输入 y、n，或逗号分隔的成功序号，例如 1,3。') from exc
    if any(i < 0 or i >= count for i in indices):
        raise ValueError('确认序号超出本批范围。')
    return indices


def migrate(args):
    require_desktop_platform()
    images = all_images()
    if not images:
        raise RuntimeError(f'目录没有可用表情文件：{EXPORT}。请先运行 --export。')
    manual = getattr(args, 'manual_import', False)
    cfg = load_json(CONFIG, None)
    if not manual:
        if not cfg:
            raise RuntimeError('请先运行 --calibrate 校准飞书「+ 添加」按钮。')
        if cfg.get('platform', 'darwin') != sys.platform:
            raise RuntimeError('校准配置来自其他系统，请在当前电脑重新运行 --calibrate。')
    state = load_json(STATE, {'attempted': {}, 'verified': {}})
    state.setdefault('attempted', {})
    state.setdefault('verified', {})
    pending, seen = [], set()
    for path in images:
        digest = fingerprint(path)
        if digest not in seen and digest not in state['verified']:
            pending.append((path, digest))
            seen.add(digest)
    if not args.retry_attempted:
        pending = [(p, h) for p, h in pending if h not in state['attempted']]
    if args.limit is not None:
        pending = pending[:args.limit]
    print(f'总文件 {len(images)}，本次准备尝试 {len(pending)}。')
    if not pending:
        return
    if not args.yes:
        print('安全模式：默认只尝试第一张。用 --yes 才会执行设置的数量。')
        pending = pending[:1]
    batch_size = getattr(args, 'batch_size', GROUP_SIZE)
    if args.pause_every:
        batch_size = min(batch_size, args.pause_every)
    if not manual and sys.platform == 'darwin':
        batch_size = 1
    batches = import_batches(pending, batch_size)
    pg = None if manual else ui_lib()
    print('请保持飞书在个人表情添加界面。每批完成后，回终端确认实际成功的图片。')
    input('按回车开始；Ctrl+C 可停止：')
    attempted_count = 0
    for index, batch in enumerate(batches, 1):
        paths = [p for p, _ in batch]
        print(f'\n批次 {index}/{len(batches)}，分组 {paths[0].parent.name}，共 {len(paths)} 张：')
        for number, path in enumerate(paths, 1):
            print(f'  {number}. {path.name}')
        if not manual:
            activate_feishu(cfg.get('app_name', '飞书'))
            time.sleep(.5)
        # Persist uncertainty before interacting: interruption may happen after submission.
        for path, digest in batch:
            state['attempted'][digest] = {'file': str(path), 'at': time.strftime('%Y-%m-%d %H:%M:%S'), 'status': 'attempted_unverified'}
        save_json(STATE, state)
        if manual:
            open_folder(paths[0].parent)
            input('请在飞书只选择上述图片并添加，完成后回到终端按回车：')
        else:
            choose_files(pg, paths, cfg)
        attempted_count += len(batch)
        while True:
            answer = input('核查飞书后确认：全部成功输入 y；部分成功输入序号如 1,3；未确认输入 n：')
            try:
                confirmed = confirmed_indices(answer, len(batch))
                break
            except ValueError as exc:
                print(exc)
        for item in confirmed:
            path, digest = batch[item]
            state['verified'][digest] = {'file': str(path), 'at': time.strftime('%Y-%m-%d %H:%M:%S'), 'status': 'user_confirmed'}
        save_json(STATE, state)
        if len(confirmed) != len(batch):
            print('本批尚有未确认图片，已暂停后续批次。核查后用 --retry-attempted 重试未确认项。')
            break
    print(f'\n本次尝试 {attempted_count} 张；累计人工确认 {len(state["verified"])} 张。状态：{STATE}')


def main():
    parser = argparse.ArgumentParser(description='微信收藏表情导出、分组并通过 macOS / Windows UI 添加到飞书个人表情')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--export', action='store_true', help='仅从微信导出到本地')
    group.add_argument('--calibrate', action='store_true', help='校准飞书添加按钮')
    group.add_argument('--import', dest='do_import', action='store_true', help='尝试自动添加到飞书')
    group.add_argument('--all', action='store_true', help='先导出再尝试自动添加')
    group.add_argument('--group-only', action='store_true', help='只整理已有图片，每组最多 50 张；不访问微信')
    group.add_argument('--list-accounts', action='store_true', help='只读列出包含表情数据库的微信账号')
    group.add_argument('--probe', action='store_true', help='只读检测 Windows 微信实际版本和账号，不读取密钥')
    parser.add_argument('--app-name', default='飞书', help='macOS 应用名称或 Windows 飞书窗口标题，国际版可填 Lark')
    parser.add_argument('--wechat-data-dir', default=None, help='微信 xwechat_files 数据根目录，默认探测用户目录和文档目录')
    parser.add_argument('--key-file', default=None, help='可选：从 UTF-8 文本读取本机已有的数据库密钥，不调用外部提取工具')
    parser.add_argument('--manual-import', action='store_true', help='打开批次目录，由用户在飞书按列表手动选择并确认')
    parser.add_argument('--batch-size', type=int, default=50, help='Windows 单次自动选择数量，1 至 50，默认 50')
    parser.add_argument('--wxid', default=None, help='微信账号目录名；存在多个账号时需要指定')
    parser.add_argument('--yes', action='store_true', help='允许批量操作；默认只测试 1 张')
    parser.add_argument('--limit', type=int, default=None, help='本次最多尝试多少张')
    parser.add_argument('--pause-every', type=int, default=0, help='兼容旧参数：进一步限制批次大小，0 表示按 --batch-size；每批始终需要核对')
    parser.add_argument('--retry-attempted', action='store_true', help='重试曾尝试的文件（可能重复）')
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error('--limit 必须大于 0')
    if args.pause_every < 0:
        parser.error('--pause-every 不能小于 0')
    if not 1 <= args.batch_size <= GROUP_SIZE:
        parser.error('--batch-size 必须在 1 至 50 之间')
    try:
        if args.list_accounts or args.probe:
            from wechat_source import find_accounts
            if args.probe:
                if sys.platform == 'win32':
                    from windows_wechat import discover_processes
                    processes = discover_processes()
                    for process in processes:
                        print('微信进程：', process)
                    if not processes:
                        print('没有发现已运行的微信，请先打开并登录。')
                else:
                    print('Windows 版本探测需要在 Windows 上运行。')
            accounts = find_accounts(args.wechat_data_dir)
            for account in accounts:
                print(f'{account["id"]}: {account["database"]}')
            if not accounts:
                raise RuntimeError('未发现表情数据库；请先登录微信并打开收藏表情面板，或指定 --wechat-data-dir。')
            return 0
        if not args.group_only:
            require_desktop_platform()
        if args.export or args.all:
            move_old_flat_exports()
            run_export(args.wechat_data_dir, args.wxid, args.key_file)
            group_exports()
        elif args.group_only or args.do_import:
            group_exports()
        if args.calibrate:
            calibrate(args.app_name)
        if args.do_import or args.all:
            migrate(args)
    except KeyboardInterrupt:
        print('\n已由用户中断。')
        return 130
    except Exception as exc:
        print(f'错误：{exc}', file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
