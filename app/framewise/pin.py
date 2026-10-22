"""A screenshot pinned above every window, to keep it in view while working.

Drag it to move it, scroll to make it more or less see-through, double-click
to edit it, right-click for more, Esc to close. Keeping it above other
windows needs the GNOME Shell extension; without it, it's a normal window.
"""

import itertools

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

_numbers = itertools.count(1)

CSS = """
.ck-pin { background: transparent; }
.ck-pin-frame { border-radius: 10px; border: 1px solid alpha(black, 0.25);
                box-shadow: 0 6px 20px alpha(black, 0.35); }
"""


class Pin(Gtk.ApplicationWindow):
    def __init__(self, app, path, on_edit=None, on_closed=None):
        self.title_text = f"framewise pin {next(_numbers)}"
        super().__init__(application=app, title=self.title_text, decorated=False)
        self.path = path
        self.on_edit = on_edit
        self.on_closed = on_closed
        self.add_css_class("ck-pin")
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        texture = Gdk.Texture.new_from_filename(str(path))
        w, h = texture.get_width(), texture.get_height()
        # At most 40% of a typical screen, never larger than the picture.
        k = min(1.0, 900 / w, 600 / h)
        picture = Gtk.Picture(paintable=texture, can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN)
        picture.set_size_request(60, 40)
        self.set_default_size(max(80, round(w * k)) + 2, max(60, round(h * k)) + 2)
        frame = Gtk.Frame(child=picture, css_classes=["ck-pin-frame"])
        frame.set_overflow(Gtk.Overflow.HIDDEN)
        self.set_child(frame)
        self.picture = picture

        drag = Gtk.GestureClick(button=1)
        drag.connect("pressed", self._pressed)
        picture.add_controller(drag)
        menu_click = Gtk.GestureClick(button=3)
        menu_click.connect("pressed", lambda _g, _n, x, y: self._menu(x, y))
        picture.add_controller(menu_click)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._scroll)
        picture.add_controller(scroll)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)

        for name, cb in (("copy", self._copy), ("edit", self._edit), ("close", self.close),
                         ("close-all", self._close_all)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, cb=cb: cb())
            self.add_action(action)
        self.connect("close-request", self._closing)

    def show_pinned(self, shell):
        self.present()
        if shell is not None and shell.has_keep_above:
            tries = [0]

            def attempt():
                # GNOME finds the window only once it is on screen: try until it does.
                tries[0] += 1
                return not shell.keep_above(self.title_text) and tries[0] < 20
            GLib.timeout_add(150, attempt)

    # ---- input ------------------------------------------------------------------------

    def _pressed(self, gesture, n_press, x, y):
        if n_press == 2:
            self._edit()
            return
        # Wayland lets the compositor move the window, started from this press.
        surface = self.get_surface()
        event = gesture.get_current_event()
        if surface is not None and event is not None:
            surface.begin_move(gesture.get_device(), 1, x, y, event.get_time())

    def _scroll(self, _c, _dx, dy):
        self.set_opacity(max(0.2, min(1.0, self.get_opacity() - dy * 0.08)))
        return True

    def _key(self, _c, keyval, _code, state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if keyval in (Gdk.KEY_c, Gdk.KEY_C) and state & Gdk.ModifierType.CONTROL_MASK:
            self._copy()
            return True
        return False

    def _menu(self, x, y):
        menu = Gio.Menu()
        menu.append("Copy", "win.copy")
        menu.append("Edit", "win.edit")
        menu.append("Close", "win.close")
        menu.append("Close all pins", "win.close-all")
        pop = Gtk.PopoverMenu(menu_model=menu, has_arrow=False)
        pop.set_parent(self.picture)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        pop.set_pointing_to(rect)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    # ---- actions ----------------------------------------------------------------------

    def _copy(self):
        from .shots import copy_image
        copy_image(self.path)

    def _edit(self):
        if self.on_edit:
            self.on_edit(str(self.path))

    def _close_all(self):
        for win in list(self.get_application().get_windows()):
            if isinstance(win, Pin):
                win.close()

    def _closing(self, *_):
        if self.on_closed:
            cb, self.on_closed = self.on_closed, None
            cb()
        return False
