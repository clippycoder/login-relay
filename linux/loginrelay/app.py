# SPDX-License-Identifier: GPL-3.0-or-later
"""The GTK 3 tray app. Every gi import in the program lives in this module.

The pieces that decide what to show (menu entries, window text and buttons, prompts) are plain data
produced by engine.py (menu_model, describe, window_actions, paste_prompt), so this file only turns
them into widgets.
"""
from __future__ import annotations

import datetime
import os
import signal
import subprocess
import sys
import threading
import traceback
from typing import Dict, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

# System tray: Ayatana first (current Debian/Ubuntu/Fedora), then the older libappindicator.
AppIndicator = None
INDICATOR_NAME = ""
INDICATOR_PROBLEM = ""
for _ns in ("AyatanaAppIndicator3", "AppIndicator3"):
    try:
        gi.require_version(_ns, "0.1")
        if _ns == "AyatanaAppIndicator3":
            from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
        else:
            from gi.repository import AppIndicator3 as AppIndicator  # noqa: E402
        INDICATOR_NAME = _ns
        break
    except (ValueError, ImportError) as _e:
        INDICATOR_PROBLEM = str(_e)
        AppIndicator = None

from . import APP_ID, VERSION, claim, installer  # noqa: E402
from . import engine as eng  # noqa: E402
from .prefs import Prefs, find_share_file, share_dirs  # noqa: E402

COMMENTS = ("Finishes browser sign-ins for Android phones that have no browser, using this computer's "
            "browser over adb or a plain USB connection.\n\nGPL-3.0-or-later")


# ---- small helpers ---------------------------------------------------------------------------

def open_in_browser(url: str) -> None:
    try:
        if Gio.AppInfo.launch_default_for_uri(url, None):
            return
    except GLib.Error:
        pass
    try:
        p = subprocess.Popen(["xdg-open", url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        threading.Thread(target=p.wait, daemon=True).start()  # reap it
    except OSError:
        traceback.print_exc(file=sys.stderr)


def _guard(fn):
    """Wraps a GLib callback so an exception is printed instead of lost; always returns False
    (remove the source)."""
    def run(*_args):
        try:
            fn()
        except Exception:
            traceback.print_exc(file=sys.stderr)
        return False
    return run


class _Timer:
    def __init__(self, delay: float, fn):
        self._fn = fn
        self._id = GLib.timeout_add(max(1, int(delay * 1000)), self._fire)

    def _fire(self):
        self._id = 0
        try:
            self._fn()
        except Exception:
            traceback.print_exc(file=sys.stderr)
        return False

    def cancel(self):
        if self._id:
            GLib.source_remove(self._id)
            self._id = 0


def _message_dialog(kind, title: str, detail: str):
    dlg = Gtk.MessageDialog(message_type=kind, buttons=Gtk.ButtonsType.NONE, text=title)
    dlg.set_property("secondary-text", detail)
    dlg.set_keep_above(True)
    return dlg


# ---- the paste-the-address prompt ------------------------------------------------------------

class PasteDialog:
    def __init__(self, app: "LoginRelayApp", flow: eng.Flow):
        self.app, self.flow = app, flow
        title, body, placeholder = eng.paste_prompt(flow)
        dlg = Gtk.Dialog(title=title)
        dlg.set_keep_above(True)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("Send to Phone", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        box = dlg.get_content_area()
        box.set_spacing(10)
        box.set_border_width(14)
        self.label = Gtk.Label(label=body)
        self.label.set_line_wrap(True)
        self.label.set_xalign(0)
        self.label.set_max_width_chars(60)
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text(placeholder)
        self.entry.set_width_chars(56)
        self.entry.set_activates_default(True)
        box.pack_start(self.label, False, False, 0)
        box.pack_start(self.entry, False, False, 0)
        dlg.connect("response", self._on_response)
        self.dlg: Optional[Gtk.Dialog] = dlg
        dlg.show_all()
        dlg.present()
        self.entry.grab_focus()

    def _on_response(self, _dlg, response):
        if response == Gtk.ResponseType.OK:
            verdict = self.app.engine.submit_pasted(self.flow, self.entry.get_text())
            if verdict != eng.OK and self.dlg is not None:
                self.label.set_text(eng.paste_feedback(verdict))
                self.entry.grab_focus()
            return
        self.close()
        self.app.engine.cancel_pasted(self.flow)

    def close(self):
        dlg, self.dlg = self.dlg, None
        if dlg is not None:
            dlg.destroy()


# ---- the engine's Host -----------------------------------------------------------------------

class GtkHost(eng.Host):
    def __init__(self, app: "LoginRelayApp"):
        self.app = app
        self._paste: Dict[eng.Flow, PasteDialog] = {}

    def post(self, fn):
        GLib.idle_add(_guard(fn))

    def call_later(self, delay, fn):
        return _Timer(delay, fn)

    def open_url(self, url):
        open_in_browser(url)

    def claim(self, scheme):
        return claim.default_manager().claim(scheme)

    def notify(self, title, body):
        n = Gio.Notification.new(title)
        n.set_body(body)
        self.app.send_notification(None, n)

    def show_error(self, title, detail):
        dlg = _message_dialog(Gtk.MessageType.WARNING, title, detail)
        dlg.add_button("OK", Gtk.ResponseType.OK)
        dlg.connect("response", lambda d, _r: d.destroy())
        dlg.show_all()
        dlg.present()

    def confirm_without_pkce(self, request):
        dlg = _message_dialog(Gtk.MessageType.WARNING, "%s is signing in without PKCE" % request.app_name,
                              "The code that passes through this computer could be used on its own to take over "
                              "the sign-in. Only continue if you trust this app and this computer.")
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("Continue Anyway", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        dlg.show_all()
        dlg.present()
        answer = dlg.run()
        dlg.destroy()
        return answer == Gtk.ResponseType.OK

    def ask_for_pasted_address(self, flow):
        self._paste[flow] = PasteDialog(self.app, flow)

    def close_pasted_address(self, flow):
        d = self._paste.pop(flow, None)
        if d is not None:
            d.close()


# ---- the activity window ---------------------------------------------------------------------

class ActivityWindow:
    def __init__(self, app: "LoginRelayApp"):
        self.app = app
        win = Gtk.ApplicationWindow(application=app, title="Login Relay")
        win.set_default_size(520, 440)
        win.set_icon_name(APP_ID)
        win.connect("delete-event", self._on_delete)
        self.win = win

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_border_width(20)
        win.add(outer)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        icon = Gtk.Image.new_from_icon_name(APP_ID, Gtk.IconSize.DIALOG)
        icon.set_pixel_size(56)
        header.pack_start(icon, False, False, 0)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.status = Gtk.Label()
        self.status.set_xalign(0)
        self.status.set_line_wrap(True)
        self.detail = Gtk.Label()
        self.detail.set_xalign(0)
        self.detail.set_line_wrap(True)
        self.detail.set_max_width_chars(60)
        self.detail.get_style_context().add_class("dim-label")
        titles.pack_start(self.status, False, False, 0)
        titles.pack_start(self.detail, False, False, 0)
        header.pack_start(titles, True, True, 0)
        outer.pack_start(header, False, False, 0)

        self.note = Gtk.Label()
        self.note.set_xalign(0)
        self.note.set_line_wrap(True)
        self.note.set_max_width_chars(70)
        self.note.set_no_show_all(True)
        outer.pack_start(self.note, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.primary = Gtk.Button()
        self.secondary = Gtk.Button()
        for b in (self.primary, self.secondary):
            b.set_no_show_all(True)
            buttons.pack_start(b, False, False, 0)
        self.primary.connect("clicked", self._clicked, 0)
        self.secondary.connect("clicked", self._clicked, 1)
        self._actions = [None, None]
        outer.pack_start(buttons, False, False, 0)

        # "Connect over" (adb or USB accessory) and, in USB mode with several phones, "Phone".
        self._choosers = {}
        self._entries = {}
        self._updating = False
        choosers = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._add_chooser(choosers, "transport", "Connect over:")
        self._add_chooser(choosers, "usbphone", "Phone:")
        outer.pack_start(choosers, False, False, 0)

        title = Gtk.Label()
        title.set_xalign(0)
        title.set_markup("<small><b>Activity</b></small>")
        title.get_style_context().add_class("dim-label")
        outer.pack_start(title, False, False, 0)

        self.buffer = Gtk.TextBuffer()
        self.view = Gtk.TextView.new_with_buffer(self.buffer)
        self.view.set_editable(False)
        self.view.set_cursor_visible(False)
        self.view.set_monospace(True)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.set_left_margin(6)
        self.view.set_right_margin(6)
        self.end_mark = self.buffer.create_mark("end", self.buffer.get_end_iter(), False)
        scroll = Gtk.ScrolledWindow()
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.set_min_content_height(180)
        scroll.add(self.view)
        outer.pack_start(scroll, True, True, 0)

    def _add_chooser(self, parent, key: str, caption: str) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        label = Gtk.Label(label=caption)
        combo = Gtk.ComboBoxText()
        box.pack_start(label, False, False, 0)
        box.pack_start(combo, False, False, 0)
        label.show()
        combo.show()
        box.set_no_show_all(True)   # shown only when the engine offers choices (see _fill_chooser)
        combo.connect("changed", self._chooser_changed, key)
        parent.pack_start(box, False, False, 0)
        self._choosers[key] = (box, combo, [])

    def _fill_chooser(self, key: str, entries) -> None:
        """Shows `entries` (MenuEntry toggles from the engine) in the drop-down, or hides it if empty."""
        box, combo, current = self._choosers[key]
        signature = [(e.label, e.action, e.arg) for e in entries]
        self._updating = True
        try:
            if signature != current:
                combo.remove_all()
                for e in entries:
                    combo.append_text(e.label)
                self._choosers[key] = (box, combo, signature)
            combo.set_active(next((i for i, e in enumerate(entries) if e.checked), -1))
            self._entries[key] = list(entries)
            box.set_visible(bool(entries))
        finally:
            self._updating = False

    def _chooser_changed(self, combo, key: str) -> None:
        if self._updating:
            return
        i = combo.get_active()
        entries = self._entries.get(key, [])
        if 0 <= i < len(entries) and not entries[i].checked:
            self.app.run_action(entries[i].action, entries[i].arg)

    def set_note(self, text: str) -> None:
        self.note.set_text(text)
        self.note.set_visible(bool(text))

    def _on_delete(self, *_args):
        if self.app.indicator is None:
            self.app.quit()       # no tray icon to come back through, so closing the window ends the app
            return True
        self.win.hide()
        return True

    def _clicked(self, _button, index):
        action = self._actions[index]
        if action:
            self.app.run_action(action)

    def show(self):
        self.update()
        self.win.show_all()
        self.win.present()

    def update(self):
        engine = self.app.engine
        status, detail = eng.describe(engine)
        self.status.set_markup('<span size="large" weight="bold">%s</span>' % GLib.markup_escape_text(status))
        self.detail.set_text(detail)
        primary, secondary, enabled = eng.window_actions(engine)
        for i, (button, spec) in enumerate(((self.primary, primary), (self.secondary, secondary))):
            self._actions[i] = spec[1] if spec else None
            button.set_visible(spec is not None)
            if spec:
                button.set_label(spec[0])
            button.set_sensitive(enabled if i == 0 else True)
        self._fill_chooser("transport", eng.transport_choices(engine))
        self._fill_chooser("usbphone", eng.usb_phone_choices(engine))
        if self.win.get_visible():
            text = "\n".join("%s  %s" % (ts.strftime("%H:%M:%S"), t) for ts, t in engine.log_lines)
            self.buffer.set_text(text)
            self.buffer.move_mark(self.end_mark, self.buffer.get_end_iter())
            self.view.scroll_mark_onscreen(self.end_mark)


# ---- the application -------------------------------------------------------------------------

class LoginRelayApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.add_main_option("url", 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Hand a return address to the running Login Relay", "ADDRESS")
        self.add_main_option("background", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Start without opening the window", None)
        self.add_main_option("verbose", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Print the activity log to stderr", None)
        self.started = False
        self.engine: Optional[eng.RelayEngine] = None
        self.prefs: Optional[Prefs] = None
        self.window: Optional[ActivityWindow] = None
        self.indicator = None
        self._menu_model = None
        self._icon_name = None
        self._tray_icons = False
        self._refresh_pending = False
        self._force_menu = False

    # -- command line (the first call is the first launch; later ones are forwarded to us)

    def do_command_line(self, command_line):
        opts = command_line.get_options_dict().end().unpack()
        url = opts.get("url")
        first = not self.started
        if first:
            self._start(bool(opts.get("verbose")))
        if url:
            self.engine.receive(url)
        else:
            extra = [a for a in command_line.get_arguments()[1:] if "://" in a]
            for a in extra:
                self.engine.receive(a)
        if first:
            self._first_window(bool(opts.get("background")), have_url=bool(url))
        elif not url and not extra:
            self.window.show()      # a second normal launch brings up the first
        return 0

    def do_shutdown(self):
        if self.engine is not None:
            try:
                self.engine.shutdown()
            except Exception:
                traceback.print_exc(file=sys.stderr)
        Gtk.Application.do_shutdown(self)

    # -- start-up

    def _start(self, verbose: bool) -> None:
        self.started = True
        self.hold()    # no window keeps the app alive; the tray does
        GLib.set_application_name("Login Relay")
        Gtk.Window.set_default_icon_name(APP_ID)
        theme = Gtk.IconTheme.get_default()
        for d in share_dirs():
            icons = os.path.join(d, "icons")
            if os.path.isdir(icons):
                theme.append_search_path(icons)

        self.prefs = Prefs()
        apk, version = eng.find_bundled()
        self.engine = eng.RelayEngine(GtkHost(self), self.prefs, bundled_apk=apk, bundled_phone_version=version)
        self.engine.verbose = verbose
        self._install_log_file()
        try:
            claim.default_manager().release_stale()   # a crash mid-login can leave a claim behind
        except Exception:
            traceback.print_exc(file=sys.stderr)

        self.window = ActivityWindow(self)
        self._setup_tray()
        self.engine.on_change = self._schedule_refresh
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_signal)
        self._do_refresh()
        self.engine.start()

    def _first_window(self, background: bool, have_url: bool) -> None:
        first_run = not self.prefs.has_launched_before
        if first_run:
            self.prefs.has_launched_before = True
        # GNOME has no tray without an extension, so the very first launch always shows the window; so
        # does a launch where no tray icon could be created (the window is then the whole interface).
        if first_run or self.indicator is None or not (background or have_url):
            self.window.show()

    def _on_signal(self, *_args):
        self.quit()
        return False

    def _install_log_file(self) -> None:
        path = os.environ.get("LOGINRELAY_LOG")
        if not path:
            return

        def write(ts: datetime.datetime, text: str) -> None:
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write("%s  %s\n" % (ts.isoformat(timespec="seconds"), text))
            except OSError:
                pass

        self.engine.log_hooks.append(write)

    # -- tray

    def _setup_tray(self) -> None:
        note = ""
        if AppIndicator is None:
            note = ("No system tray support was found (%s). Install gir1.2-ayatanaappindicator3-0.1 (and on "
                    "GNOME the AppIndicator extension) to get a tray icon. This window stays open so you can "
                    "still use Login Relay." % (INDICATOR_PROBLEM or "AppIndicator is not installed"))
            self.engine.log(note)
        else:
            try:
                tray_dir = find_share_file("tray")
                category = AppIndicator.IndicatorCategory.APPLICATION_STATUS
                if tray_dir:
                    ind = AppIndicator.Indicator.new_with_path(APP_ID, "loginrelay-tray-off", category, tray_dir)
                else:
                    ind = AppIndicator.Indicator.new(APP_ID, APP_ID, category)
                ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
                ind.set_title("Login Relay")
                self.indicator = ind
                self._tray_icons = bool(tray_dir)
                self.engine.log("Tray icon: %s." % INDICATOR_NAME)
            except Exception as e:  # missing watcher, bad theme path, ...
                note = "The tray icon couldn't be created (%s). This window stays open instead." % e
                self.engine.log(note)
                self.indicator = None
        self.window.set_note(note)

    def _schedule_refresh(self, force_menu: bool = False) -> None:
        self._force_menu = self._force_menu or force_menu
        if not self._refresh_pending:
            self._refresh_pending = True
            GLib.idle_add(_guard(self._do_refresh))

    def _do_refresh(self) -> None:
        self._refresh_pending = False
        force, self._force_menu = self._force_menu, False
        if self.window is not None:
            self.window.update()
        if self.indicator is None:
            return
        engine = self.engine
        kind = engine.phase.kind
        if self._tray_icons:
            name = ("loginrelay-tray-active" if kind == eng.WORKING
                    else "loginrelay-tray" if kind == eng.READY else "loginrelay-tray-off")
        else:
            name = APP_ID
        status = eng.describe(engine)[0]
        if name != self._icon_name:
            self._icon_name = name
            self.indicator.set_icon_full(name, status)
        self.indicator.set_title("Login Relay: " + status)
        # The tray can't tell us when its menu opens, so the menu is rebuilt whenever what it shows changes.
        model = eng.menu_model(engine, claim.autostart_enabled())
        if force or model != self._menu_model:
            self._menu_model = model
            menu = Gtk.Menu()
            show_item = self._fill_menu(menu, model)
            menu.show_all()
            self.indicator.set_menu(menu)
            if show_item is not None:
                try:
                    self.indicator.set_secondary_activate_target(show_item)  # middle-click
                except Exception:
                    pass

    def _fill_menu(self, menu: Gtk.Menu, entries):
        show_item = None
        for e in entries:
            if e.kind == "separator":
                item = Gtk.SeparatorMenuItem()
            elif e.kind in ("status", "hint"):
                label = ("●  " + e.label) if e.kind == "status" else e.label
                item = Gtk.MenuItem.new_with_label(label)
                item.set_sensitive(False)
            elif e.kind == "toggle":
                item = Gtk.CheckMenuItem.new_with_label(e.label)
                item.set_active(e.checked)
                item.connect("activate", self._on_menu, e.action, e.arg)
            elif e.kind == "submenu":
                item = Gtk.MenuItem.new_with_label(e.label)
                sub = Gtk.Menu()
                self._fill_menu(sub, e.children)
                item.set_submenu(sub)
            else:
                item = Gtk.MenuItem.new_with_label(e.label)
                item.connect("activate", self._on_menu, e.action, e.arg)
                if e.action == eng.A_SHOW:
                    show_item = item
            menu.append(item)
        return show_item

    def _on_menu(self, _item, action, arg):
        self.run_action(action, arg)

    # -- actions (from the tray menu and the window's buttons)

    def run_action(self, action: str, arg: str = "") -> None:
        e = self.engine
        if action == eng.A_DOWNLOAD:
            self.confirm_download_platform_tools()
        elif action == eng.A_CHOOSE_ADB:
            self.choose_adb()
        elif action == eng.A_INSTALL:
            e.install_phone_app()
        elif action == eng.A_OPEN:
            e.open_on_phone()
        elif action == eng.A_CANCEL:
            e.cancel_active("Cancelled on the computer.", notify_phone=True)
        elif action == eng.A_CHECK:
            e.check_again()
        elif action == eng.A_SHOW:
            self.window.show()
        elif action == eng.A_LAUNCH_AT_LOGIN:
            self.toggle_launch_at_login()
        elif action == eng.A_NOTIFY:
            self.toggle_notifications()
        elif action == eng.A_ABOUT:
            self.about()
        elif action == eng.A_QUIT:
            self.quit()
        elif action == eng.A_SELECT_PHONE:
            e.select_device(arg)
        elif action == eng.A_SELECT_USB:
            e.select_usb(arg)
        elif action == eng.A_SET_TRANSPORT:
            e.set_transport(arg)

    def confirm_download_platform_tools(self) -> None:
        dlg = _message_dialog(Gtk.MessageType.QUESTION, "Download Android platform-tools?",
                              "This downloads Google's platform-tools (which include adb) directly from "
                              "dl.google.com, checks them against Google's published checksum, and keeps them in "
                              "Login Relay's own folder. They're covered by the Android SDK License.")
        dlg.add_button("Download", Gtk.ResponseType.OK)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("View License", Gtk.ResponseType.HELP)

        def on_response(d, response):
            d.destroy()
            if response == Gtk.ResponseType.OK:
                self.engine.download_platform_tools()
            elif response == Gtk.ResponseType.HELP:
                open_in_browser(installer.LICENSE_URL)

        dlg.connect("response", on_response)
        dlg.show_all()
        dlg.present()

    def choose_adb(self) -> None:
        dlg = Gtk.FileChooserDialog(title="Choose the adb program", action=Gtk.FileChooserAction.OPEN)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("Open", Gtk.ResponseType.OK)
        dlg.set_current_folder(os.path.expanduser("~"))
        dlg.set_keep_above(True)

        def on_response(d, response):
            path = d.get_filename() if response == Gtk.ResponseType.OK else None
            d.destroy()
            if path:
                self.engine.choose_adb(path)

        dlg.connect("response", on_response)
        dlg.show_all()
        dlg.present()

    def toggle_launch_at_login(self) -> None:
        enable = not claim.autostart_enabled()
        try:
            claim.set_autostart(enable)
            self.prefs.launch_at_login = enable
        except OSError as e:
            self.engine.show_error("Couldn't change Launch at Login", str(e))
        self._schedule_refresh(force_menu=True)

    def toggle_notifications(self) -> None:
        self.prefs.notifications = not self.prefs.notifications
        if self.prefs.notifications:
            self.engine.notify("Login Relay", "You'll see a note here when an app asks to sign in.")
        self._schedule_refresh(force_menu=True)

    def about(self) -> None:
        dlg = Gtk.AboutDialog()
        dlg.set_program_name("Login Relay")
        dlg.set_version(VERSION)
        dlg.set_comments(COMMENTS)
        dlg.set_license_type(Gtk.License.GPL_3_0)
        dlg.set_logo_icon_name(APP_ID)
        dlg.connect("response", lambda d, _r: d.destroy())
        dlg.show()
        dlg.present()


def main(argv=None) -> int:
    GLib.set_prgname(APP_ID)
    return LoginRelayApp().run(sys.argv if argv is None else argv)
