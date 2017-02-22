import sublime
import traceback

from . import neo
from .edit import Edit

KEYMAP = {
    'backspace': '\b',
    'enter': '\n',
    'escape': '\033',
    'space': ' ',
    'tab': '\t',
    'up': '\033[A',
    'down': '\033[B',
    'right': '\033[C',
    'left': '\033[D',
}

def keymap(key):
    if '+' in key and key != '+':
        mods, key = key.rsplit('+', 1)
        mods = mods.split('+')
        if mods == ['ctrl']:
            b = ord(key)
            if b >= 63 and b < 96:
                return chr((b - 64) % 128)

    return KEYMAP.get(key, key)


def copy_sel(sel):
    if isinstance(sel, sublime.View):
        sel = sel.sel()
    return [(r.a, r.b) for r in sel]


try:
    _views
except NameError:
    _views = {}


class ViewMeta:
    @classmethod
    def get(cls, view, create=True, exact=True):
        vid = view.id()
        m = _views.get(vid)
        if not m and create:
            try:
                m = cls(view)
            except Exception:
                traceback.print_exc()
                return
            _views[vid] = m
        elif m and exact and m.view != view:
            return None

        return m

    def __init__(self, view):
        self.view = view
        self.last_sel = None

    def sel_changed(self):
        new_sel = copy_sel(self.view)
        changed = new_sel != self.last_sel
        self.last_sel = new_sel
        return changed

    def visual(self, mode, a, b):
        view = self.view
        regions = []
        sr, sc = a
        er, ec = b

        a = view.text_point(sr, sc)
        b = view.text_point(er, ec)

        if mode == 'V':
            # visual line mode
            if a > b:
                start = view.line(a).b
                end = view.line(b).a
            else:
                start = view.line(a).a
                end = view.line(b).b

            regions.append((start, end))
        elif mode == 'v':
            # visual mode
            if a > b:
                a += 1
            else:
                b += 1
            regions.append((a, b))
        elif mode == '\x16':
            # visual block mode
            left = min(sc, ec)
            right = max(sc, ec) + 1
            top = min(sr, er)
            bot = max(sr, er)
            end = view.text_point(top, right)

            for i in range(top, bot + 1):
                line = view.line(view.text_point(i, 0))
                _, end = view.rowcol(line.b)
                if left <= end:
                    a = view.text_point(i, left)
                    b = view.text_point(i, min(right, end))
                    regions.append((a, b))
        else:
            regions.append((a, b))

        return [sublime.Region(*r) for r in regions]

class ActualVim(ViewMeta):
    def __init__(self, view):
        super().__init__(view)
        if view.settings().get('actual_proxy'):
            return

        s = {
            'actual_intercept': True,
            'actual_mode': True,
            # it's most likely a buffer will start in command mode
            'inverse_caret_state': True,
        }
        for k, v in s.items():
            view.settings().set(k, v)

        self.buf = None

    @classmethod
    def reload_classes(cls):
        # reload classes by creating a new blank instance without init and overlaying dicts
        for vid, view in _views.items():
            new = cls.__new__(cls)
            nd = {}
            # copy view dict first to get attrs, new second to get methods
            nd.update(view.__dict__)
            nd.update(new.__dict__)
            new.__dict__.update(nd)
            _views[vid] = new

    @property
    def actual(self):
        return self.view and self.view.settings().get('actual_mode')

    def activate(self):
        # first activate
        if self.buf is None:
            self.buf = neo.vim.buf_new()
            self.buf[:] = self.view.substr(sublime.Region(0, self.view.size())).split('\n')
            self.sel_to_vim()

        neo.vim.buf_activate(self.buf)

    def update_caret(self):
        mode = neo.vim.mode
        wide = (mode not in neo.INSERT_MODES + neo.VISUAL_MODES)
        self.view.settings().set('inverse_caret_state', wide)

    def sync_to_vim(self):
        pass

    def sync_from_vim(self, edit=None):
        pass

    def sel_to_vim(self):
        # defensive, could affect perf
        self.activate()

        if self.sel_changed():
            # single selection for now...
            # TODO: block
            # TODO multiple select vim plugin integration
            sel = self.view.sel()[0]
            vim = neo.vim
            b = self.view.rowcol(sel.b)
            if sel.b == sel.a:
                vim.select(b)
            else:
                a = self.view.rowcol(sel.a)
                vim.select(a, b)

            self.sel_from_vim()
            self.update_caret()

    def sel_from_vim(self, edit=None):
        a, b = neo.vim.sel
        new_sel = self.visual(neo.vim.mode, a, b)

        def select():
            sel = self.view.sel()
            sel.clear()
            sel.add_all(new_sel)
            self.sel_changed()

        if edit is None:
            Edit.defer(self.view, select)
        else:
            edit.callback(select)

    def press(self, key):
        # TODO: can we ever reach here without being the active buffer?
        # defensive, could affect perf
        self.activate()
        if self.buf is None:
            return

        neo.vim.press(keymap(key))
        # TODO: trigger UI update on vim event, not here
        # TODO: global UI change is GROSS, do deltas if possible
        text = '\n'.join(self.buf[:])
        everything = sublime.Region(0, self.view.size())
        if self.view.substr(everything) != text:
            with Edit(self.view) as edit:
                edit.replace(everything, text)

        self.sel_from_vim()

        # (trigger this somewhere else? vim mode change callback?)
        self.update_caret()

    def close(self):
        if self.buf is not None:
            neo.vim.buf_close(self.buf)

    def set_path(self, path):
        self.buf.name = path


class ActualPanel:
    def __init__(self, actual):
        self.actual = actual
        self.vim = actual.vim
        self.view = actual.view
        self.panel = None

    def close(self):
        if self.panel:
            self.panel.close()

    def show(self, char):
        window = self.view.window()
        self.panel = window.show_input_panel('Vim', char, self.on_done, None, self.on_cancel)
        settings = self.panel.settings()
        settings.set('actual_intercept', True)
        settings.set('actual_proxy', self.view.id())
        ActualVim.views[self.panel.id()] = self.actual

    def on_done(self, text):
        self.vim.press('enter')
        self.vim.panel = None

    def on_cancel(self):
        self.vim.press('escape')
        self.vim.panel = None

ActualVim.reload_classes()
