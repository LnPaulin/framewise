"""The recording editor: opens after a recording stops, before saving.

Preview on top, transport and timeline below, sound tracks at the bottom.
Trim the ends, Shift+drag to cut parts out, set each sound track's volume,
then Save, Save as, Copy or Delete. Edits never touch the original take,
so undo always works; the edit is stored next to the take as edit.json.
"""

import json
import shutil
import subprocess
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import edit_model as em  # noqa: E402
from . import controls_patch, export  # noqa: E402
from . import denoise  # noqa: E402
from . import silence  # noqa: E402
from . import speech  # noqa: E402
from . import motion  # noqa: E402
from .filmstrip import Filmstrip, ZoomLane, make_thumbnails  # noqa: E402
from .editor_export import ExportMixin, copy_file_to_clipboard, format_length  # noqa: E402
from .editor_style import StyleMixin  # noqa: E402
from .zoomview import ZoomView  # noqa: E402
from .player import Player  # noqa: E402
from .state import DATA_DIR, settings  # noqa: E402
from .ui import format_time  # noqa: E402

TRACK_LABELS = {"system": ("System sound", "audio-speakers-symbolic"),
                "mic": ("Microphone", "audio-input-microphone-symbolic")}

CSS = """
.ck-editor-stage { background: #000; }
.ck-editor-time { font-feature-settings: "tnum"; }
.ck-editor-hint { font-size: 0.85em; }
"""


def probe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


class Editor(ExportMixin, StyleMixin, Adw.ApplicationWindow):
    def __init__(self, app, take_dir, on_closed=None, on_saved=None):
        super().__init__(application=app, title="Recording")
        self.on_saved = on_saved      # called after each successful save
        self.set_default_size(1280, 860)
        self.take_dir = Path(take_dir)
        self.take = self.take_dir / "take.mkv"
        self.on_closed = on_closed
        try:
            self.info = json.loads((self.take_dir / "take.json").read_text())
        except (OSError, ValueError):
            self.info = {}
        self.duration = probe_duration(self.take)
        self.video_size = export.video_size(self.take)
        crop_fraction = self.info.get("crop")
        self.crop_px = export.crop_pixels(crop_fraction, self.video_size) if crop_fraction else None
        self.tracks = export.track_names(self.info, export.audio_track_count(self.take))

        is_new = not (self.take_dir / "edit.json").exists()
        self.doc = em.load(self.take_dir / "edit.json", self.duration)
        if is_new and "mic" in export.track_names(self.info, export.audio_track_count(self.take)):
            # New recordings start with noise removal as it was last left.
            self.doc["denoise"]["mic"] = bool(settings.get("denoise_mic"))
            self.doc["denoise"]["strength"] = settings.get("denoise_strength") or "normal"
        self._init_export(is_new)
        if self.info.get("cursor") == "hidden":
            # The recording left the real pointer out: the editor draws it.
            self.doc["pointer"]["drawn"] = True
        self.fps = self.info.get("fps", 60)
        # Where the pointer was while recording (needs the add-on, 0.5+).
        self.pointer = export.load_pointer(self.take_dir)
        self.area_samples = export.area_samples(self.pointer, crop_fraction)
        self.clicks = (self.pointer or {}).get("clicks", [])
        self.typing = export.area_typing(self.pointer, crop_fraction)
        if self.pointer and not self.doc["zoom"]["generated"]:
            # The first set of zooms comes from the clicks, once; after that
            # they belong to the user.
            self.doc["zoom"]["segments"] = motion.segments_from_activity(self.clicks, self.typing,
                                                                         self.duration)
            self.doc["zoom"]["generated"] = True
            self.doc = em.normalize(self.doc, self.duration)
            em.save(self.take_dir / "edit.json", self.doc)
        self._views_timer = None
        self.undo_stack, self.redo_stack = [], []
        self.saved_file = None        # path of the last save, if any
        self.saved_doc = None         # the document that file was made from
        self.job = None

        self._init_style()
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._clean_jobs = {}
        clean = denoise.clean_path(self.take_dir, self.doc["denoise"]["strength"])
        self.player = None
        self._clean_strength_loaded = self.doc["denoise"]["strength"] if clean.exists() else None
        self._make_player(clean if clean.exists() else None)

        self._build()
        self._refresh()
        self._update_views()
        if self.doc["denoise"]["mic"] and not clean.exists() and "mic" in self.tracks:
            # Noise removal is on (remembered from last time): clean the
            # microphone now instead of on the first edit.
            self._ensure_clean(self.doc["denoise"]["strength"], lambda: None)
        make_thumbnails(self.take, self.take_dir / "thumbs", self.crop_px,
                        self.strip.set_thumbnails, fps=self.info.get("fps", 60))
        self.connect("close-request", self._on_close_request)

    def _make_player(self, mic_clean):
        """(Re)creates the preview player, e.g. once a cleaned microphone exists."""
        position, playing = 0.0, False
        if self.player is not None:
            position, playing = self.player.position(), self.player.playing
            self.player.close()
        self.player = Player(self.take, self.video_size, self.crop_px, self.tracks, mic_clean=mic_clean)
        self.player.set_document(self.doc)
        self.player.on_position = self._on_position
        self.player.on_state = self._on_play_state
        self.has_clean_mic = mic_clean is not None
        if hasattr(self, "picture"):
            self.picture.set_paintable(self.player.paintable)
            self.picture.time_source = self.player.position
            self.picture.on_click = self.player.toggle
            GLib.timeout_add(250, lambda: (self.player.seek(position),
                                           self.player.play() if playing else None, False)[-1])

    # ---- layout ----------------------------------------------------------------

    def _build(self):
        header = Adw.HeaderBar()
        self.undo_btn = Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text="Undo (Ctrl+Z)")
        self.undo_btn.connect("clicked", lambda *_: self.undo())
        self.redo_btn = Gtk.Button(icon_name="edit-redo-symbolic", tooltip_text="Redo (Ctrl+Shift+Z)")
        self.redo_btn.connect("clicked", lambda *_: self.redo())
        header.pack_start(self.undo_btn)
        header.pack_start(self.redo_btn)
        self.title = Adw.WindowTitle(title="Recording")
        header.set_title_widget(self.title)

        save = Gtk.Button(label="Save", css_classes=["suggested-action"], tooltip_text="Save (Ctrl+S)")
        save.connect("clicked", lambda *_: self.save())
        menu = Gio.Menu()
        menu.append("Save As…", "win.save-as")
        menu.append("Copy", "win.copy")
        menu.append("Save as GIF…", "win.save-gif")
        menu.append("Copy as GIF", "win.copy-gif")
        menu.append("Delete Recording", "win.delete")
        more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, tooltip_text="More")
        header.pack_end(more)
        header.pack_end(save)
        header.pack_end(self._build_export_button())
        self.save_btn = save
        for name, cb in (("save-as", self.save_as), ("copy", self.copy), ("delete", self.delete),
                         ("save-gif", self.save_gif_as), ("copy-gif", self.copy_gif)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, cb=cb: cb())
            self.add_action(action)

        # Stage
        self.picture = ZoomView(self.player.paintable)
        self.picture.add_css_class("ck-editor-stage")
        self.picture.time_source = self.player.position
        self.picture.on_click = self.player.toggle
        self.picture.on_pick = self._on_pick_spot
        self._load_controls_patch()

        # Transport
        self.play_btn = Gtk.Button(icon_name="media-playback-start-symbolic",
                                   css_classes=["circular", "flat"], tooltip_text="Play (Space)")
        self.play_btn.connect("clicked", lambda *_: self.player.toggle())
        self.time_label = Gtk.Label(css_classes=["ck-editor-time", "dim-label"])
        self.cut_btn = Gtk.Button(css_classes=["destructive-action"], visible=False,
                                  tooltip_text="Delete selection (Delete)")
        self.cut_btn.set_child(self._icon_label("edit-cut-symbolic", "Cut"))
        self.cut_btn.connect("clicked", lambda *_: self.cut_selection())
        transport = Gtk.Box(spacing=10, margin_start=12, margin_end=12, margin_top=8)
        transport.append(self.play_btn)
        transport.append(self.time_label)
        transport.append(Gtk.Box(hexpand=True))
        transport.append(self._build_add_button())
        transport.append(self._build_style_button())
        transport.append(self._build_pointer_button())
        transport.append(self._build_zoom_button())
        transport.append(self._build_silence_button())
        transport.append(self.cut_btn)

        # Timeline
        self.strip = Filmstrip(self.duration)
        self.strip.set_margin_start(12)
        self.strip.set_margin_end(12)
        self.strip.on_scrub = self._on_scrub
        self.strip.on_trim = self._on_trim
        self.strip.on_select = self._on_select
        self.strip.on_restore = self.restore_cut
        self.strip.on_speed_restore = self.restore_speed
        self.strip.on_suggestion = self._cut_suggestion
        hint = Gtk.Label(label="Drag to scrub  ·  Drag the red handles to trim  ·  "
                               "Shift-drag to cut  ·  Click an orange marker "
                               "to restore a cut",
                         css_classes=["dim-label", "ck-editor-hint"], margin_top=4)

        # Sound tracks
        tracks = Gtk.Box(spacing=24, margin_start=12, margin_end=12, margin_top=10, margin_bottom=12,
                         halign=Gtk.Align.CENTER)
        self.track_widgets = {}
        for name in self.tracks:
            label, icon = TRACK_LABELS.get(name, (name, "audio-x-generic-symbolic"))
            mute = Gtk.ToggleButton(icon_name=icon, css_classes=["flat", "circular"],
                                    tooltip_text=f"Mute {label.lower()}")
            mute.connect("toggled", self._on_mute, name)
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 200, 1)
            scale.set_size_request(180, -1)
            scale.set_draw_value(False)
            scale.add_mark(100, Gtk.PositionType.BOTTOM, None)
            scale.connect("value-changed", self._on_gain, name)
            value = Gtk.Label(css_classes=["ck-editor-time", "dim-label"], width_chars=5)
            box = Gtk.Box(spacing=6)
            box.append(mute)
            box.append(Gtk.Label(label=label))
            box.append(scale)
            box.append(value)
            if name == "mic":
                box.append(self._build_denoise_controls())
            if name == "system" and "mic" in self.tracks:
                box.append(self._build_sync_controls())
            tracks.append(box)
            self.track_widgets[name] = (mute, scale, value)
        if not self.tracks:
            tracks.append(Gtk.Label(label="No audio tracks", css_classes=["dim-label"]))

        # Saving progress
        self.progress = Gtk.ProgressBar(hexpand=True, valign=Gtk.Align.CENTER)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.job and self.job.cancel())
        self.progress_bar = Gtk.Box(spacing=12, margin_start=12, margin_end=12, margin_bottom=10,
                                    visible=False)
        self.progress_bar.append(Gtk.Label(label="Saving…"))
        self.progress_bar.append(self.progress)
        self.progress_bar.append(cancel)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self.picture)
        body.append(transport)
        body.append(self._build_suggestion_bar())
        body.append(self.strip)
        self.lane = ZoomLane(self.strip)
        self.lane.set_margin_start(12)
        self.lane.set_margin_end(12)
        self.lane.on_change = self._on_zoom_lane
        self.lane.on_select = self._on_zoom_selected
        body.append(self.lane)
        body.append(self._build_zoom_bar())
        body.append(self._build_overlay_lanes())
        body.append(self._build_overlay_bar())
        body.append(hint)
        body.append(tracks)
        body.append(self.progress_bar)

        self.toasts = Adw.ToastOverlay(child=body)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(self.toasts)
        self.set_content(view)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

    # ---- silences ---------------------------------------------------------------

    def _build_silence_button(self):
        self.silence_analysis = None
        self.silence_found = []
        pop_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=12,
                           margin_bottom=12, margin_start=12, margin_end=12, width_request=300)
        self._build_idle_section(pop_body)
        pop_body.append(Gtk.Separator(margin_top=6, margin_bottom=6))
        pop_body.append(Gtk.Label(label="Silence removal", xalign=0, css_classes=["heading"]))
        pop_body.append(Gtk.Label(
            label="Finds the pauses where nobody is speaking. They're shown in yellow on the "
                  "timeline; cut them all at once, then put back any you want with the orange lines.",
            xalign=0, wrap=True, max_width_chars=40, css_classes=["dim-label", "caption"]))

        pop_body.append(Gtk.Label(label="Sensitivity", xalign=0))
        levels = Gtk.Box(css_classes=["linked"], homogeneous=True)
        self.silence_levels = {}
        first = None
        for key, label in (("low", "Low"), ("medium", "Medium"), ("high", "High")):
            b = Gtk.ToggleButton(label=label, tooltip_text={
                "low": "Only clear silences", "medium": "Pauses between sentences",
                "high": "Also quiet breaths and murmurs"}[key])
            if first is None:
                first = b
            else:
                b.set_group(first)
            b.set_active(key == (settings.get("silence_sensitivity") or "medium"))
            b.connect("toggled", lambda btn, k=key: btn.get_active() and (
                settings.set("silence_sensitivity", k), self._update_silences()))
            levels.append(b)
            self.silence_levels[key] = b
        pop_body.append(levels)

        self.pause_scale = self._labeled_scale(pop_body, "Shortest pause to cut", 0.3, 3.0, 0.1,
                                               settings.get("silence_min_pause") or 0.8, "s",
                                               "silence_min_pause")
        self.keep_scale = self._labeled_scale(pop_body, "Keep around words", 0.05, 0.5, 0.05,
                                              settings.get("silence_keep") or 0.2, "s", "silence_keep")
        self.silence_status = Gtk.Label(xalign=0, css_classes=["dim-label"])
        pop_body.append(self.silence_status)
        self.silence_apply = Gtk.Button(label="Remove All", css_classes=["destructive-action", "pill"])
        self.silence_apply.connect("clicked", lambda *_: self._apply_silences())
        pop_body.append(self.silence_apply)
        pop_body.append(Gtk.Separator(margin_top=6, margin_bottom=6))
        self._build_filler_section(pop_body)

        # Scrollable: with idle moments, silences and filler words it's tall.
        scroller = Gtk.ScrolledWindow(child=pop_body, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      propagate_natural_height=True, propagate_natural_width=True,
                                      max_content_height=640)
        popover = Gtk.Popover(child=scroller)
        popover.connect("show", lambda *_: self._open_cleanup())
        self.silence_popover = popover
        button = Gtk.MenuButton(popover=popover,
                                tooltip_text="Speed up idle moments, remove silence and filler words")
        button.set_child(self._icon_label("edit-clear-all-symbolic", "Cleanup"))
        return button

    # ---- background noise -------------------------------------------------------------

    def _build_denoise_controls(self):
        self.denoise_toggle = Gtk.ToggleButton(
            tooltip_text="Reduce keyboard clicks, fan noise and hum on the microphone track")
        self.denoise_toggle.set_child(self._icon_label("audio-input-microphone-symbolic",
                                                       "Noise Reduction"))
        self.denoise_toggle.connect("toggled", self._on_denoise)
        self.denoise_strength = Gtk.DropDown.new_from_strings(["Normal", "Strong"])
        self.denoise_strength.set_tooltip_text("Strong removes more noise but can thin the voice slightly")
        self.denoise_strength.connect("notify::selected", self._on_denoise_strength)
        self.denoise_spinner = Gtk.Spinner(visible=False)
        box = Gtk.Box(spacing=6, margin_start=12)
        box.append(self.denoise_toggle)
        box.append(self.denoise_strength)
        box.append(self.denoise_spinner)
        return box

    def _ensure_clean(self, strength, then):
        """Makes the cleaned microphone track if needed, then calls then()."""
        path = denoise.clean_path(self.take_dir, strength)
        if path.exists():
            if not self.has_clean_mic or self._clean_strength_loaded != strength:
                self._clean_strength_loaded = strength
                self._make_player(path)
            then()
            return
        # One job per strength: slider moves while it runs just wait for it.
        pending = self._clean_jobs.get(strength)
        if pending is not None:
            pending.append(then)
            return
        self._clean_jobs[strength] = [then]
        self.denoise_spinner.set_visible(True)
        self.denoise_spinner.start()

        def done(result):
            if getattr(self, "_closing", False):
                return
            waiting = self._clean_jobs.pop(strength, [])
            if not self._clean_jobs:
                self.denoise_spinner.stop()
                self.denoise_spinner.set_visible(False)
            if result is None:
                self._toast("Background noise couldn't be removed")
                if self.doc["denoise"]["mic"] and self.doc["denoise"]["strength"] == strength:
                    doc = em.clone(self.doc)
                    doc["denoise"]["mic"] = False
                    self._commit(doc, remember=False)
                for then_ in waiting:   # e.g. a save that was waiting
                    then_()
                return
            # Only switch the preview if this is still the strength wanted.
            if self.doc["denoise"]["strength"] == strength:
                self._clean_strength_loaded = strength
                self._make_player(result)
            for then_ in waiting:
                then_()

        denoise.make_clean_async(self.take, self.tracks.index("mic"), self.take_dir, strength, done)

    def _on_denoise(self, button):
        if getattr(self, "_refreshing", False):
            return
        on = button.get_active()
        settings.set("denoise_mic", on)
        doc = em.clone(self.doc)
        doc["denoise"]["mic"] = on
        self._commit(doc)  # the cleaned track is made in the background

    def _on_denoise_strength(self, dropdown, _p):
        if getattr(self, "_refreshing", False):
            return
        strength = "strong" if dropdown.get_selected() == 1 else "normal"
        settings.set("denoise_strength", strength)
        doc = em.clone(self.doc)
        doc["denoise"]["strength"] = strength
        self._commit(doc)

    # ---- zoom on click -----------------------------------------------------------------

    def _build_zoom_button(self):
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=12,
                       margin_bottom=12, margin_start=12, margin_end=12, width_request=300)
        body.append(Gtk.Label(label="Zoom", xalign=0, css_classes=["heading"]))
        self.zoom_switch = Adw.SwitchRow(title="Auto zoom")
        group = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        group.append(self.zoom_switch)
        body.append(group)
        self.zoom_switch.connect("notify::active", self._on_zoom_enabled)
        row = Gtk.Box(spacing=8)
        row.append(Gtk.Label(label="Zoom level", xalign=0, hexpand=True))
        self.zoom_amount_label = Gtk.Label(css_classes=["ck-editor-time", "dim-label"])
        row.append(self.zoom_amount_label)
        body.append(row)
        self.zoom_amount = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, *motion.AMOUNT_RANGE, 0.1)
        self.zoom_amount.set_draw_value(False)
        self.zoom_amount.connect("value-changed", self._on_zoom_amount)
        body.append(self.zoom_amount)
        regen = Gtk.Button(label="Regenerate Zooms", css_classes=["flat"])
        regen.connect("clicked", lambda *_: self._regenerate_zooms())
        regen.set_sensitive(bool(self.pointer))
        body.append(regen)
        note = ("Zooms follow your pointer. Add one by clicking the zoom lane under the "
                "timeline; drag a zoom to move it or its ends to resize it.")
        if not self.pointer:
            note = ("This recording has no click track, so there are no automatic zooms. "
                    "You can still add zooms by clicking the zoom lane under the timeline.")
        body.append(Gtk.Label(label=note, xalign=0, wrap=True, max_width_chars=40,
                              css_classes=["dim-label", "caption"]))
        button = Gtk.MenuButton(popover=Gtk.Popover(child=body), tooltip_text="Zoom")
        button.set_child(self._icon_label("zoom-in-symbolic", "Zoom"))
        return button

    def _build_zoom_bar(self):
        self.zoom_follow = Gtk.ToggleButton(label="Follow Cursor")
        self.zoom_spot = Gtk.ToggleButton(label="Set Focus Point", group=self.zoom_follow)
        self.zoom_follow.connect("toggled", self._on_zoom_target)
        # "clicked", not "toggled": pressing it again must start picking again.
        self.zoom_spot.connect("clicked", lambda *_: self._start_picking())
        remove = Gtk.Button(label="Remove", css_classes=["flat"])
        remove.connect("clicked", lambda *_: self._remove_zoom())
        targets = Gtk.Box(css_classes=["linked"])
        targets.append(self.zoom_follow)
        targets.append(self.zoom_spot)
        self.zoom_hint = Gtk.Label(css_classes=["dim-label"], xalign=0, hexpand=True)
        box = Gtk.Box(spacing=10, margin_start=12, margin_end=12, margin_top=4)
        box.append(Gtk.Label(label="Selected zoom:", css_classes=["heading"]))
        box.append(targets)
        box.append(self.zoom_hint)
        box.append(remove)
        self.zoom_bar = Gtk.Revealer(child=box, reveal_child=False,
                                     transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        return self.zoom_bar

    def _zoom_doc(self, **changes):
        doc = em.clone(self.doc)
        doc["zoom"].update(changes)
        return doc

    def _on_zoom_enabled(self, row, _p):
        if getattr(self, "_refreshing", False):
            return
        self._commit(self._zoom_doc(enabled=row.get_active()))

    def _on_zoom_amount(self, scale):
        self.zoom_amount_label.set_label(f"{scale.get_value():.1f}×")
        if getattr(self, "_refreshing", False):
            return
        merge = bool(self.undo_stack) and getattr(self, "_last_gain_track", None) == "zoom-amount"
        self._commit(self._zoom_doc(amount=round(scale.get_value(), 2)), remember=not merge,
                     gain_track="zoom-amount")

    def _regenerate_zooms(self):
        self.lane.select(None)
        self._commit(self._zoom_doc(segments=motion.segments_from_activity(self.clicks, self.typing,
                                                                           self.duration),
                                    enabled=True))
        self._toast("Zooms regenerated from clicks and typing")

    def _on_zoom_lane(self, segments, done):
        if not done:
            # Show the drag live without an undo step per pixel.
            if getattr(self, "_zoom_before", None) is None:
                self._zoom_before = self.doc
            self.doc = em.normalize(self._zoom_doc(segments=segments), self.duration)
            self._update_views()
            return
        before, self._zoom_before = getattr(self, "_zoom_before", None), None
        after = self._zoom_doc(segments=segments, enabled=True)
        if before is not None:
            self.doc = before
        self._commit(after)

    def _on_zoom_selected(self, index):
        if index is not None and getattr(self, "_selected_overlay", None):
            # One selection at a time: put down any selected caption, picture or blur.
            kind = self._selected_overlay[0]
            self.overlay_lanes[kind].select(None)
        self.zoom_bar.set_reveal_child(index is not None)
        if index is None:
            self.picture.set_picking(False)
            return
        seg = self.doc["zoom"]["segments"][index] if index < len(self.doc["zoom"]["segments"]) else None
        if seg is None:
            return
        self._refreshing = True
        just_added = self.lane.just_added
        (self.zoom_spot if seg.get("focus") or just_added else self.zoom_follow).set_active(True)
        self._refreshing = False
        if just_added:
            # A zoom added by hand asks straight away where it should look.
            self._start_picking()
        else:
            self.picture.set_picking(False)
            self.zoom_hint.set_label("This zoom looks at a chosen spot" if seg.get("focus")
                                     else "This zoom follows the cursor")
        # Show the zoom: put the playhead in it.
        self.player.seek(min(seg["end"], seg["start"] + motion.RAMP_IN + 0.1))

    def _on_zoom_target(self, button):
        if getattr(self, "_refreshing", False) or self.lane.selected is None:
            return
        i = self.lane.selected
        if self.zoom_follow.get_active():
            segs = [dict(s) for s in self.doc["zoom"]["segments"]]
            if segs[i].get("focus"):
                segs[i]["focus"] = None
                self._commit(self._zoom_doc(segments=segs))
            self.picture.set_picking(False)
            self.zoom_hint.set_label("This zoom follows the cursor")

    def _start_picking(self):
        i = self.lane.selected
        if i is None or i >= len(self.doc["zoom"]["segments"]):
            return
        if self.player.playing:
            self.player.pause()
        self._refreshing = True
        self.zoom_spot.set_active(True)
        self._refreshing = False
        seg = self.doc["zoom"]["segments"][i]
        self.picture.set_picking(True, seg.get("focus"), self.doc["zoom"]["amount"])
        self.zoom_hint.set_label("Click the preview to set the focus point. "
                                 "The frame shows the visible area.")

    def _on_pick_spot(self, fx, fy):
        i = self.lane.selected
        self.picture.set_picking(False)
        if i is None:
            return
        segs = [dict(s) for s in self.doc["zoom"]["segments"]]
        segs[i]["focus"] = [round(fx, 4), round(fy, 4)]
        self._commit(self._zoom_doc(segments=segs))
        self.zoom_hint.set_label("✓ Focus point set. Click Set Focus Point again to change it.")
        # Show the result: jump into the zoom once the new view is computed.
        seg = self.doc["zoom"]["segments"][i]
        GLib.timeout_add(150, lambda: (self.player.seek(
            min(seg["end"], seg["start"] + motion.RAMP_IN + 0.15)), False)[1])

    def _remove_zoom(self):
        i = self.lane.selected
        if i is None:
            return
        segs = [dict(s) for s in self.doc["zoom"]["segments"]]
        del segs[i]
        self.lane.select(None)
        self._commit(self._zoom_doc(segments=segs))

    def _update_views(self):
        """The preview's finished frame and zoom (see StyleMixin._update_scene)."""
        self._update_scene()

    # ---- marked suggestions (pauses, filler words) -----------------------------------

    def _build_suggestion_bar(self):
        self.suggest_visible = {"silence": False, "filler": False}
        self.suggest_label = Gtk.Label(xalign=0, hexpand=True)
        cut_all = Gtk.Button(label="Remove All", css_classes=["destructive-action", "pill"],
                             tooltip_text="Remove every marked segment")
        cut_all.connect("clicked", lambda *_: self._cut_all_suggestions())
        hide = Gtk.Button(label="Dismiss", css_classes=["flat"], tooltip_text="Clear the markers")
        hide.connect("clicked", lambda *_: self._hide_suggestions())
        box = Gtk.Box(spacing=10, margin_start=12, margin_end=12, margin_top=6)
        box.append(self.suggest_label)
        box.append(hide)
        box.append(cut_all)
        self.suggest_bar = Gtk.Revealer(child=box, reveal_child=False,
                                        transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        return self.suggest_bar

    def _open_cleanup(self):
        self._open_idle()
        if not self.tracks:
            # No sound to listen to: only idle moments apply.
            self.silence_status.set_label("This recording has no sound.")
            self.silence_apply.set_sensitive(False)
            for b in self.silence_levels.values():
                b.set_sensitive(False)
            self._show_fillers()
            return
        self.suggest_visible = {"silence": True, "filler": True}
        self._open_silences()
        self._show_fillers()
        self._update_suggestion_bar()

    def _not_sped_up(self, ranges):
        """Marks inside a sped-up part are already handled: they aren't shown."""
        speedups = self.doc.get("speedups") or []

        def inside(a, b):
            return sum(max(0.0, min(b, y) - max(a, x)) for x, y, _r in speedups) >= 0.5 * (b - a)
        return [r for r in ranges if not inside(*r)]

    def _update_suggestion_bar(self):
        parts = []
        silences = self._not_sped_up(self.silence_found) if self.suggest_visible["silence"] else []
        fillers = self._not_sped_up(self.filler_found) if self.suggest_visible["filler"] else []
        self.strip.set_suggestions(silences, "silence")
        self.strip.set_suggestions(fillers, "filler")
        if fillers:
            parts.append(f"<span foreground='#9d5cff'>●</span> {len(fillers)} filler "
                         f"word{'s' if len(fillers) != 1 else ''}")
        if silences:
            parts.append(f"<span foreground='#f5b300'>●</span> {len(silences)} "
                         f"pause{'s' if len(silences) != 1 else ''}")
        if parts:
            self.suggest_label.set_markup("  ·  ".join(parts) +
                                          "  <span alpha='60%'>marked. Click one on the timeline to "
                                          "cut just that one.</span>")
        self.suggest_bar.set_reveal_child(bool(parts))

    def _hide_suggestions(self):
        self.suggest_visible = {"silence": False, "filler": False}
        self._update_suggestion_bar()

    def _cut_suggestion(self, kind, rng):
        doc = em.clone(self.doc)
        doc["cuts"].append(list(rng))
        self._commit(doc)
        self.player.seek(em.snap_to_kept(self.doc, rng[1]))
        toast = Adw.Toast(title="Filler word cut" if kind == "filler" else "Pause cut",
                          button_label="Undo", timeout=3)
        toast.connect("button-clicked", lambda *_: self.undo())
        self.toasts.add_toast(toast)

    def _cut_all_suggestions(self):
        ranges = []
        if self.suggest_visible["silence"]:
            ranges += self._not_sped_up(self.silence_found)
        if self.suggest_visible["filler"]:
            ranges += self._not_sped_up(self.filler_found)
        if not ranges:
            return
        before = em.finished_duration(self.doc)
        doc = em.clone(self.doc)
        doc["cuts"].extend([list(r) for r in ranges])
        self._commit(doc)
        saved = before - em.finished_duration(self.doc)
        toast = Adw.Toast(title=f"Cut {len(ranges)} marked part{'s' if len(ranges) != 1 else ''} · "
                                f"{saved:.1f} s shorter", button_label="Undo", timeout=6)
        toast.connect("button-clicked", lambda *_: self.undo())
        self.toasts.add_toast(toast)

    def _refresh_suggestions(self):
        """After an edit: drop marks that are now cut, keep the rest."""
        if not hasattr(self, "suggest_visible"):
            return
        if self.silence_analysis is not None:
            self._update_silences()
        self._show_fillers()

    # ---- idle moments ---------------------------------------------------------------

    def _build_idle_section(self, box):
        self.idle_activity = None        # per-frame activity, once measured
        self.idle_found = []
        self.idle_busy = False
        box.append(Gtk.Label(label="Idle moments", xalign=0, css_classes=["heading"]))
        box.append(Gtk.Label(
            label="Speeds up the moments where nothing happens: no voice, no sound, no "
                  "typing and nothing changing on screen. Click a sped-up part on the "
                  "timeline to play it at normal speed again.",
            xalign=0, wrap=True, max_width_chars=40, css_classes=["dim-label", "caption"]))
        speeds = Gtk.Box(css_classes=["linked"], homogeneous=True)
        self.idle_speeds = {}
        first = None
        saved = settings.get("idle_speed") or 4
        for value, label in ((2, "2×"), (4, "4×"), (8, "8×"), (0, "Cut")):
            b = Gtk.ToggleButton(label=label, tooltip_text="Remove them" if not value
                                 else f"Play them {value} times faster")
            if first is None:
                first = b
            else:
                b.set_group(first)
            b.set_active(value == saved)
            b.connect("toggled", lambda btn, v=value: btn.get_active() and (
                settings.set("idle_speed", v), self._update_idle()))
            speeds.append(b)
            self.idle_speeds[value] = b
        box.append(speeds)
        self.idle_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=40, css_classes=["dim-label"])
        box.append(self.idle_status)
        self.idle_apply = Gtk.Button(label="Speed Up All", css_classes=["suggested-action", "pill"],
                                     sensitive=False)
        self.idle_apply.connect("clicked", lambda *_: self._apply_idle())
        box.append(self.idle_apply)

    def _idle_speed(self):
        return next((v for v, b in self.idle_speeds.items() if b.get_active()), 4)

    def _open_idle(self):
        if self.idle_activity is not None:
            self._update_idle()
            return
        if self.idle_busy:
            return
        self.idle_busy = True
        self.idle_status.set_label("Looking for idle moments…")
        self.idle_apply.set_sensitive(False)
        from . import idle

        def done(activity):
            self.idle_busy = False
            if getattr(self, "_closing", False):
                return
            if activity is None:
                self.idle_status.set_label("The recording couldn't be analyzed.")
                return
            self.idle_activity = activity
            self._update_idle()

        ignore = controls_patch.pixels(self.info, self.video_size, self.info.get("crop"))
        idle.analyse_async(self.take, self.take_dir, self.duration, self.crop_px, self.tracks, done,
                           ignore_px=ignore)

    def _load_controls_patch(self):
        """The clean top bar over the recording controls, made in the background."""
        rect = controls_patch.in_area(self.info, self.info.get("crop"))
        if not rect:
            return

        def work():
            path = controls_patch.patch_file(self.take_dir, self.info, self.video_size, self.info.get("crop"))
            GLib.idle_add(ready, path)

        def ready(path):
            if path and not getattr(self, "_closing", False):
                try:
                    self.picture.set_patch(Gdk.Texture.new_from_filename(str(path)), rect)
                except GLib.Error:
                    pass
            return False

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _update_idle(self):
        """The idle parts not already sped up or cut, for the chosen speed."""
        if self.idle_activity is None:
            return
        from . import idle
        parts = idle.idle_parts(self.idle_activity, within=tuple(self.doc["trim"]))
        handled = [(a, b) for a, b, _r in self.doc["speedups"]] + [tuple(c) for c in self.doc["cuts"]]

        def covered(a, b):
            done = sum(max(0.0, min(b, y) - max(a, x)) for x, y in handled)
            return done >= 0.5 * (b - a)
        self.idle_found = [p for p in parts if not covered(*p)]
        speed = self._idle_speed()
        total = sum(b - a for a, b in self.idle_found)
        saved = total if not speed else total * (1 - 1 / speed)
        n = len(self.idle_found)
        if not n:
            self.idle_status.set_label("No idle moments found." if not handled
                                       else "No other idle moments found.")
        else:
            self.idle_status.set_label(f"{n} idle moment{'s' if n != 1 else ''} found · "
                                       f"{format_length(total)} → {format_length(total - saved)}")
        self.idle_apply.set_label("Cut All" if not speed else "Speed Up All")
        for css in ("suggested-action", "destructive-action"):
            self.idle_apply.remove_css_class(css)
        self.idle_apply.add_css_class("destructive-action" if not speed else "suggested-action")
        self.idle_apply.set_sensitive(bool(n))

    def _apply_idle(self):
        if not self.idle_found:
            return
        speed = self._idle_speed()
        before = em.finished_duration(self.doc)
        doc = em.clone(self.doc)
        if speed:
            doc["speedups"].extend([[a, b, speed] for a, b in self.idle_found])
        else:
            doc["cuts"].extend([[a, b] for a, b in self.idle_found])
        count = len(self.idle_found)
        self.silence_popover.popdown()
        self._commit(doc)
        saved = before - em.finished_duration(self.doc)
        what = f"Sped up {count} idle moment{'s' if count != 1 else ''}" if speed \
            else f"Cut {count} idle moment{'s' if count != 1 else ''}"
        toast = Adw.Toast(title=f"{what} · {saved:.1f} s shorter", button_label="Undo", timeout=6)
        toast.connect("button-clicked", lambda *_: self.undo())
        self.toasts.add_toast(toast)

    def restore_speed(self, index):
        """A sped-up part plays at normal speed again (clicked on the timeline)."""
        if index is None or index >= len(self.doc["speedups"]):
            return
        a = self.doc["speedups"][index][0]
        doc = em.clone(self.doc)
        del doc["speedups"][index]
        self._commit(doc)
        self.player.seek(em.snap_to_kept(self.doc, a))
        toast = Adw.Toast(title="Back to normal speed", button_label="Undo", timeout=4)
        toast.connect("button-clicked", lambda *_: self.undo())
        self.toasts.add_toast(toast)

    # ---- filler words ---------------------------------------------------------------

    def _build_filler_section(self, box):
        self.filler_words = None   # transcript words, once listened
        self.filler_found = []
        self.filler_busy = False
        box.append(Gtk.Label(label="Remove filler words", xalign=0, css_classes=["heading"]))
        box.append(Gtk.Label(
            label="Finds “um”, “uh”, “hmm” and “erm” by listening to what you say. "
                  "They're shown in purple. Speech recognition runs on this computer.",
            xalign=0, wrap=True, max_width_chars=40, css_classes=["dim-label", "caption"]))
        self.filler_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=40,
                                       css_classes=["dim-label"])
        self.filler_progress = Gtk.ProgressBar(visible=False)
        self.filler_button = Gtk.Button(css_classes=["pill"])
        self.filler_button.connect("clicked", lambda *_: self._filler_clicked())
        box.append(self.filler_status)
        box.append(self.filler_progress)
        box.append(self.filler_button)
        cached = self.take_dir / "transcript.json"
        if cached.exists():
            try:
                self.filler_words = json.loads(cached.read_text()).get("words")
            except (OSError, ValueError):
                self.filler_words = None
        self._show_fillers()

    def _show_fillers(self):
        # Also called while the window is still being built (before the strip exists).
        if not hasattr(self, "filler_button") or not hasattr(self, "strip"):
            return
        has_mic = "mic" in self.tracks
        if self.filler_busy:
            return
        self.filler_button.remove_css_class("destructive-action")
        self.filler_button.set_sensitive(has_mic)
        if not has_mic:
            self.filler_status.set_label("This recording has no microphone track.")
            self.filler_button.set_label("Detect Filler Words")
            return
        if self.filler_words is None:
            self.filler_status.set_label("" if speech.is_ready() else
                                         f"The first time, this downloads speech recognition "
                                         f"({speech.DOWNLOAD_SIZE}).")
            self.filler_button.set_label("Detect Filler Words")
            return
        found = []
        analysis = self.silence_analysis
        db = analysis.db if analysis is not None else None
        thr = analysis.threshold("medium") if analysis is not None else None
        for a, b in speech.filler_cuts(self.filler_words, db, thr):
            kept = sum(max(0.0, min(b, y) - max(a, x)) for x, y in em.kept_segments(self.doc))
            if kept >= 0.05:
                found.append((a, b))
        self.filler_found = found
        self._update_suggestion_bar()
        if found:
            self.filler_status.set_label(f"{len(found)} filler word{'s' if len(found) != 1 else ''} found")
            self.filler_button.set_label("Remove All")
            self.filler_button.add_css_class("destructive-action")
        else:
            self.filler_status.set_label("No filler words found.")
            self.filler_button.set_label("Detect Again")

    def _filler_clicked(self):
        if self.filler_found and self.filler_words is not None:
            self._apply_fillers()
        elif not speech.is_ready():
            self._offer_speech_download()
        else:
            self._listen()

    def _offer_speech_download(self):
        if not speech.can_install():
            self.filler_status.set_label("This needs one Ubuntu package first. In a terminal, run:\n"
                                         "sudo apt install python3-venv")
            return
        self.silence_popover.popdown()
        dialog = Adw.AlertDialog(
            heading="Download speech recognition?",
            body=f"To find filler words, the editor needs speech recognition ({speech.DOWNLOAD_SIZE}, "
                 "downloaded once). It runs on this computer: your recordings are never uploaded. "
                 "You can remove it later to free the space.")
        dialog.add_response("cancel", "Not Now")
        dialog.add_response("download", "Download")
        dialog.set_response_appearance("download", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda _d, r: r == "download" and self._install_speech())
        dialog.present(self)

    def _install_speech(self):
        self.filler_busy = True
        self.filler_button.set_sensitive(False)
        self.filler_progress.set_visible(True)
        toast = Adw.Toast(title="Downloading speech recognition…", timeout=0)
        self.toasts.add_toast(toast)

        def progress(label, fraction):
            self.filler_status.set_label(f"{label}…")
            self.filler_progress.set_fraction(fraction)
            toast.set_title(f"{label}…")

        def done(ok, message):
            toast.dismiss()
            self.filler_busy = False
            self.filler_progress.set_visible(False)
            if ok:
                self._toast("Speech recognition is ready")
                self._listen()
            else:
                self.filler_status.set_label(f"The download didn't finish: {message[-200:]}")
                self._toast("Speech recognition couldn't be installed")
                self.filler_button.set_sensitive(True)

        speech.install_async(progress, done)

    def _listen(self):
        self.filler_busy = True
        self.filler_button.set_sensitive(False)
        self.filler_progress.set_visible(True)
        self.filler_progress.set_fraction(0)
        self.filler_status.set_label("Transcribing…")

        def progress(f):
            self.filler_progress.set_fraction(f)
            self.filler_status.set_label(f"Transcribing… {round(f * 100)} %")

        def done(words, message):
            self.filler_busy = False
            self.filler_progress.set_visible(False)
            if words is None:
                self.filler_status.set_label(f"Couldn't listen to the recording: {message[-200:]}")
                self.filler_button.set_sensitive(True)
                return
            self.filler_words = words
            self.suggest_visible["filler"] = True
            self._show_fillers()

        speech.transcribe_async(self.take, self.tracks.index("mic"),
                                self.take_dir / "transcript.json", progress, done)

    def _apply_fillers(self):
        before = em.finished_duration(self.doc)
        doc = em.clone(self.doc)
        doc["cuts"].extend([list(r) for r in self.filler_found])
        count = len(self.filler_found)
        self.silence_popover.popdown()
        self._commit(doc)
        saved = before - em.finished_duration(self.doc)
        toast = Adw.Toast(title=f"Cut {count} filler word{'s' if count != 1 else ''} · "
                                f"{saved:.1f} s shorter", button_label="Undo", timeout=6)
        toast.connect("button-clicked", lambda *_: self.undo())
        self.toasts.add_toast(toast)

    def _labeled_scale(self, box, title, lo, hi, step, value, unit, key):
        row = Gtk.Box(spacing=8)
        row.append(Gtk.Label(label=title, xalign=0, hexpand=True))
        value_label = Gtk.Label(css_classes=["ck-editor-time", "dim-label"])
        row.append(value_label)
        box.append(row)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
        scale.set_draw_value(False)

        def changed(sc):
            value_label.set_label(f"{sc.get_value():.2f} {unit}".replace(".00", ""))
            settings.set(key, round(sc.get_value(), 2))
            self._update_silences()

        scale.connect("value-changed", changed)
        scale.set_value(float(value))
        value_label.set_label(f"{float(value):.2f} {unit}".replace(".00", ""))
        box.append(scale)
        return scale

    def _open_silences(self):
        if self.silence_analysis is not None:
            self._update_silences()
            return
        self.silence_status.set_label("Analyzing audio…")
        self.silence_apply.set_sensitive(False)
        if "mic" in self.tracks:
            index = self.tracks.index("mic")
        else:
            index = 0 if len(self.tracks) == 1 else None

        def done(analysis):
            self.silence_analysis = analysis
            if analysis is None:
                self.silence_status.set_label("The audio couldn't be analyzed")
                return
            self._update_silences()
            self._show_fillers()  # filler cuts use the same loudness

        silence.analyse_async(self.take, index, done, track_count=len(self.tracks))

    def _sensitivity(self):
        return next((k for k, b in self.silence_levels.items() if b.get_active()), "medium")

    def _update_silences(self):
        if self.silence_analysis is None or not hasattr(self, "keep_scale"):
            return
        found = self.silence_analysis.silences(
            self._sensitivity(), self.pause_scale.get_value(), self.keep_scale.get_value(),
            within=tuple(self.doc["trim"]))
        # Only what isn't cut already.
        probe = em.clone(self.doc)
        new = []
        for a, b in found:
            kept = sum(max(0.0, min(b, y) - max(a, x)) for x, y in em.kept_segments(probe))
            if kept >= 0.1:
                new.append((a, b))
        self.silence_found = new
        total = sum(b - a for a, b in new)
        if new:
            self.silence_status.set_label(
                f"{len(new)} pause{'s' if len(new) != 1 else ''} found · {total:.1f} s shorter")
        else:
            self.silence_status.set_label("No pauses found with these settings")
        self.silence_apply.set_sensitive(bool(new))
        self._update_suggestion_bar()

    def _apply_silences(self):
        if not self.silence_found:
            return
        before = em.finished_duration(self.doc)
        doc = em.clone(self.doc)
        doc["cuts"].extend([list(r) for r in self.silence_found])
        count = len(self.silence_found)
        self.silence_popover.popdown()
        self._commit(doc)
        saved = before - em.finished_duration(self.doc)
        toast = Adw.Toast(title=f"Cut {count} silent part{'s' if count != 1 else ''} · "
                                f"{saved:.1f} s shorter", button_label="Undo", timeout=6)
        toast.connect("button-clicked", lambda *_: self.undo())
        self.toasts.add_toast(toast)

    @staticmethod
    def _icon_label(icon, text):
        box = Gtk.Box(spacing=6)
        box.append(Gtk.Image(icon_name=icon))
        box.append(Gtk.Label(label=text))
        return box

    # ---- edits ----------------------------------------------------------------------

    def _commit(self, new_doc, remember=True, gain_track=None):
        """Every change goes through here: normalize, undo, save edit.json, redraw."""
        self._last_gain_track = gain_track
        new_doc = em.normalize(new_doc, self.duration)
        if new_doc == self.doc:
            return
        if remember:
            self.undo_stack.append(self.doc)
            self.redo_stack.clear()
        self.doc = new_doc
        em.save(self.take_dir / "edit.json", self.doc)
        dn = self.doc["denoise"]
        if dn["mic"] and (not self.has_clean_mic or self._clean_strength_loaded != dn["strength"]):
            # Turned on, strength changed, or undo/redo: make or load the
            # cleaned track this state needs (one job at a time).
            self._ensure_clean(dn["strength"], lambda: None)
        self.player.set_document(self.doc)
        self._refresh()
        self._refresh_suggestions()
        self._update_views()

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(self.doc)
            self._commit(self.undo_stack.pop(), remember=False)
            self.strip.clear_selection()
            self._refresh()

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(self.doc)
            self._commit(self.redo_stack.pop(), remember=False)
            self.strip.clear_selection()
            self._refresh()

    def _on_trim(self, start, end, done):
        if not done:
            # While dragging, show the trim without adding an undo step each pixel.
            if not hasattr(self, "_trim_before") or self._trim_before is None:
                self._trim_before = self.doc
            doc = em.clone(self.doc)
            doc["trim"] = [start, end]
            self.doc = em.normalize(doc, self.duration)
            self.player.set_document(self.doc)
            self.player.seek(start if abs(start - self._trim_before["trim"][0]) > 1e-6 else end)
            self._refresh()
            return
        before = getattr(self, "_trim_before", None)
        self._trim_before = None
        if before is not None and before != self.doc:
            after, self.doc = self.doc, before
            self._commit(after)

    def _on_select(self, selection):
        self.cut_btn.set_visible(selection is not None)

    def cut_selection(self):
        if not self.strip.selection:
            return
        a, b = self.strip.selection
        doc = em.clone(self.doc)
        doc["cuts"].append([a, b])
        self.strip.clear_selection()
        self.cut_btn.set_visible(False)
        self._commit(doc)
        # The parts on either side now join; park the playhead on the join.
        self.player.seek(em.snap_to_kept(self.doc, b))

    def restore_cut(self, index):
        if index is None or index >= len(self.doc["cuts"]):
            return
        a, _b = self.doc["cuts"][index]
        doc = em.clone(self.doc)
        del doc["cuts"][index]
        self._commit(doc)
        self.player.seek(em.snap_to_kept(self.doc, a))
        toast = Adw.Toast(title="Cut restored", button_label="Undo", timeout=4)
        toast.connect("button-clicked", lambda *_: self.undo())
        self.toasts.add_toast(toast)

    def _on_mute(self, button, name):
        doc = em.clone(self.doc)
        doc["muted"][name] = button.get_active()
        self._commit(doc)

    # ---- sound sync --------------------------------------------------------------------

    def _build_sync_controls(self):
        """Moves the system sound against the voice: for singing or talking
        along to a video when the two don't line up in the recording."""
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10, margin_bottom=10,
                       margin_start=12, margin_end=12)
        body.append(Gtk.Label(label="Audio sync", xalign=0, css_classes=["heading"]))
        delay = float(self.info.get("system_delay") or 0.0)
        note = ("If system audio plays ahead of the voice, move it later; "
                "if it plays behind, move it earlier.")
        if delay > 0.01:
            note += (f" The output device latency ({delay * 1000:.0f} ms) was corrected "
                     "during recording.")
        body.append(Gtk.Label(label=note, xalign=0, wrap=True, max_width_chars=40,
                              css_classes=["dim-label", "caption"]))
        self.sync_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -500, 500, 5)
        self.sync_scale.set_size_request(280, -1)
        self.sync_scale.set_draw_value(False)
        self.sync_scale.add_mark(0, Gtk.PositionType.BOTTOM, None)
        self.sync_scale.connect("value-changed", self._on_sync)
        body.append(self.sync_scale)
        row = Gtk.Box(spacing=6)
        self.sync_value = Gtk.Label(xalign=0, hexpand=True, css_classes=["ck-editor-time"])
        row.append(self.sync_value)
        for label, step in (("−10 ms", -10), ("+10 ms", 10)):
            b = Gtk.Button(label=label, css_classes=["flat"])
            b.connect("clicked", lambda _b, st=step: self.sync_scale.set_value(self.sync_scale.get_value() + st))
            row.append(b)
        reset = Gtk.Button(label="Reset", css_classes=["flat"])
        reset.connect("clicked", lambda *_: self.sync_scale.set_value(0))
        row.append(reset)
        body.append(row)
        self.sync_button = Gtk.MenuButton(label="Sync", tooltip_text="Adjust system audio timing",
                                          popover=Gtk.Popover(child=body), css_classes=["flat"])
        return self.sync_button

    def _sync_text(self, ms):
        if abs(ms) < 1:
            return "No offset"
        return f"System audio {ms:+.0f} ms"

    def _on_sync(self, scale):
        if getattr(self, "_refreshing", False):
            return
        doc = em.clone(self.doc)
        doc["sync"] = {"system": round(scale.get_value() / 1000.0, 3)}
        merge = bool(self.undo_stack) and getattr(self, "_last_gain_track", None) == "sync"
        self._commit(doc, remember=not merge, gain_track="sync")

    def _on_gain(self, scale, name):
        if getattr(self, "_refreshing", False):
            return
        doc = em.clone(self.doc)
        doc["gain"][name] = scale.get_value() / 100.0
        # Slider moves merge into one undo step if they follow each other.
        merge = bool(self.undo_stack) and getattr(self, "_last_gain_track", None) == name
        self._commit(doc, remember=not merge, gain_track=name)

    # ---- display ---------------------------------------------------------------------

    def _refresh(self):
        self._refreshing = True
        self.strip.set_document(self.doc)
        self.undo_btn.set_sensitive(bool(self.undo_stack))
        self.redo_btn.set_sensitive(bool(self.redo_stack))
        for name, (mute, scale, value) in self.track_widgets.items():
            mute.set_active(self.doc["muted"][name])
            scale.set_value(self.doc["gain"][name] * 100)
            value.set_label(f"{round(self.doc['gain'][name] * 100)} %")
            scale.set_sensitive(not self.doc["muted"][name])
        self._update_time(self.player.position())
        if hasattr(self, "sync_scale"):
            ms = float((self.doc.get("sync") or {}).get("system", 0.0)) * 1000
            self.sync_scale.set_value(ms)
            self.sync_value.set_label(self._sync_text(ms))
            self.sync_button.set_label("Sync" if abs(ms) < 1 else f"Sync {ms:+.0f} ms")
        if hasattr(self, "denoise_toggle"):
            self.denoise_toggle.set_active(self.doc["denoise"]["mic"])
            self.denoise_strength.set_selected(1 if self.doc["denoise"]["strength"] == "strong" else 0)
        z = self.doc["zoom"]
        self.zoom_switch.set_active(z["enabled"])
        self.zoom_amount.set_value(z["amount"])
        self.zoom_amount_label.set_label(f"{z['amount']:.1f}×")
        self.lane.set_segments(z["segments"] if z["enabled"] else [], self.clicks)
        self._refresh_style()
        self._refresh_export()
        self._refreshing = False

    def _update_time(self, t):
        f = em.to_finished(self.doc, em.snap_to_kept(self.doc, t)) or 0.0
        self.time_label.set_label(f"{format_time(f)} / {format_time(em.finished_duration(self.doc))}")

    def _on_position(self, t):
        self.strip.set_position(t)
        self.lane.queue_draw()
        self._update_time(t)

    def _on_play_state(self, playing):
        self.play_btn.set_icon_name("media-playback-pause-symbolic" if playing
                                    else "media-playback-start-symbolic")

    def _on_scrub(self, t):
        if self.player.playing:
            self.player.pause()
        self.player.seek(t)
        if not self.strip.selection:
            self.cut_btn.set_visible(False)

    def _on_key(self, _c, keyval, _code, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if keyval == Gdk.KEY_space:
            self.player.toggle()
        elif ctrl and keyval in (Gdk.KEY_z, Gdk.KEY_Z):
            self.redo() if shift else self.undo()
        elif ctrl and keyval in (Gdk.KEY_y, Gdk.KEY_Y):
            self.redo()
        elif ctrl and keyval in (Gdk.KEY_s, Gdk.KEY_S):
            self.save_as() if shift else self.save()
        elif ctrl and keyval in (Gdk.KEY_c, Gdk.KEY_C):
            self.copy()
        elif keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace) and self.strip.selection:
            self.cut_selection()
        else:
            return False
        return True

    # ---- saving ------------------------------------------------------------------------

    def _export(self, dest, on_done, gif=False):
        if self.job:
            return
        dn = self.doc["denoise"]
        if dn["mic"] and dn["strength"] in self._clean_jobs:
            # Still cleaning the microphone: save once that is ready.
            self._toast("Removing background noise first…")
            self._clean_jobs[dn["strength"]].append(lambda: self._export(dest, on_done, gif))
            return
        self.player.pause()
        self.progress.set_fraction(0)
        self.progress_bar.set_visible(True)
        self.save_btn.set_sensitive(False)
        doc = em.clone(self.doc)

        def done(ok, message):
            self.job = None
            self.progress_bar.set_visible(False)
            self.save_btn.set_sensitive(True)
            if ok and not gif:
                self.saved_doc = doc
            if not ok and message == export.TOO_LONG:
                fit = doc["export"]["fit_mb"]
                self._toast(f"This is too long to fit in {fit} MB. Choose a bigger size "
                            "in Export, a faster speed, or cut it shorter.")
                return
            on_done(ok, message)

        self.job = export.save_async(self.take, dest, self.info.get("crop"), done, doc=doc,
                                     take_info=self.info, duration=self.duration,
                                     on_progress=self.progress.set_fraction, gif=gif)

    def save(self):
        if not self.saved_file or not Path(self.saved_file).exists():
            self._export(export.destination(settings.get("folder")), self._on_saved)
            return
        saved = Path(self.saved_file)
        if self.saved_doc == self.doc:
            # Nothing changed since: saving again would make the same file.
            toast = Adw.Toast(title=f"Already saved, no changes since: {saved.name}", button_label="Show")
            toast.connect("button-clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(
                GLib.filename_to_uri(str(saved.parent)), None))
            self.toasts.add_toast(toast)
            return
        dialog = Adw.AlertDialog(
            heading="Replace Saved Video?",
            body=f"“{saved.name}” was saved before your latest changes. Replace it, or keep it "
                 "and save your changes as a new video.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("new", "Save as New")
        dialog.add_response("replace", "Replace")
        dialog.set_response_appearance("replace", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("replace")
        dialog.set_close_response("cancel")

        def answered(_d, response):
            if response == "replace":
                self._export(saved, self._on_saved)
            elif response == "new":
                self._export(export.destination(settings.get("folder")), self._on_saved)
        dialog.connect("response", answered)
        dialog.present(self)

    def _on_saved(self, ok, message):
        if not ok:
            if message != "cancelled":
                self._toast(f"The recording could not be saved: {message[:120]}")
            return
        self.saved_file = message
        self.title.set_subtitle(Path(message).name)
        toast = Adw.Toast(title=f"Saved to {Path(message).parent.name}/{Path(message).name}",
                          button_label="Show")
        toast.connect("button-clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(
            GLib.filename_to_uri(str(Path(message).parent)), None))
        self.toasts.add_toast(toast)
        if self.on_saved:
            self.on_saved()

    def save_as(self):
        dialog = Gtk.FileDialog(title="Save recording as", initial_name=(
            Path(self.saved_file).name if self.saved_file
            else export.destination(settings.get("folder")).name))
        dialog.set_initial_folder(Gio.File.new_for_path(str(Path(settings.get("folder")).expanduser())))

        def picked(d, result):
            try:
                f = d.save_finish(result)
            except GLib.Error:
                return
            path = Path(f.get_path())
            if path.suffix.lower() != ".mp4":
                path = path.with_suffix(".mp4")
            self._export(path, self._on_saved)

        dialog.save(self, None, picked)

    def copy(self):
        """Saves a copy in the app's cache and puts the file on the clipboard."""
        clips = DATA_DIR / "clips"
        clips.mkdir(parents=True, exist_ok=True)
        dest = clips / export.destination(clips).name

        def done(ok, message):
            if not ok:
                if message != "cancelled":
                    self._toast("The recording could not be copied")
                return
            if copy_file_to_clipboard(message):
                self._toast("Recording copied to the clipboard")
            else:
                self._toast("Copying requires wl-clipboard")

        self._export(dest, done)

    def delete(self):
        if self.saved_file and self.saved_doc == self.doc:
            self._discard_and_close()
            return
        dialog = Adw.AlertDialog(heading="Delete this recording?",
                                 body="It has not been saved anywhere yet." if not self.saved_file
                                 else "The latest changes have not been saved. "
                                      "The file you saved earlier stays where it is.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _d, r: r == "delete" and self._discard_and_close())
        dialog.present(self)

    def _toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text))

    # ---- closing ---------------------------------------------------------------------------

    def _on_close_request(self, *_):
        if self.job:
            self._toast("Wait for the export to finish, or cancel it")
            return True
        if self.saved_file and self.saved_doc == self.doc:
            self._cleanup()
            return False
        dialog = Adw.AlertDialog(heading="Save this recording?",
                                 body="If you don't save it, it will be deleted."
                                 if not self.saved_file else
                                 "Your latest changes haven't been saved.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("discard", "Delete" if not self.saved_file else "Don't save")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")

        def response(_d, r):
            if r == "discard":
                self._discard_and_close()
            elif r == "save":
                def after(ok, message):
                    self._on_saved(ok, message)
                    if ok:
                        self._discard_and_close()
                dest = Path(self.saved_file) if self.saved_file else export.destination(settings.get("folder"))
                self._export(dest, after)

        dialog.connect("response", response)
        dialog.present(self)
        return True

    def _discard_and_close(self):
        self._cleanup()
        self.destroy()

    def _cleanup(self):
        """The take lives only while its editor is open (saved files stay)."""
        self._closing = True
        self.player.close()
        shutil.rmtree(self.take_dir, ignore_errors=True)
        if self.on_closed:
            cb, self.on_closed = self.on_closed, None
            cb()
