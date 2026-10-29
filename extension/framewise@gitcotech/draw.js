// Draw on screen: pen, highlighter, arrow, rectangle and spotlight over the
// live screen, for presenting and for recordings (screen and area
// recordings capture it; a window recording shows the window alone).
//
// The shortcut (or "Draw on Screen" in the menu) starts drawing: the mouse
// draws instead of clicking, and a toolbar shows at the top. The shortcut
// again goes back to using the apps with the drawings left on screen; Esc
// clears them and stops. Keys while drawing:
//   P pen · H highlighter · A arrow · R rectangle · S spotlight
//   1-5 colours · Ctrl+Z undo · Delete clear · Tab hide the toolbar

import Cairo from 'cairo';
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {matches, prettyKeys, shortcutFor} from './keys.js';

const COLORS = [
    ['#e01b24', [0.878, 0.106, 0.141]],
    ['#f6d32d', [0.965, 0.827, 0.176]],
    ['#33d17a', [0.2, 0.82, 0.478]],
    ['#3584e4', [0.208, 0.518, 0.894]],
    ['#ffffff', [1, 1, 1]],
];
// [id, label, key]
const TOOLS = [
    ['pen', 'Pen', Clutter.KEY_p],
    ['highlighter', 'Highlighter', Clutter.KEY_h],
    ['arrow', 'Arrow', Clutter.KEY_a],
    ['rect', 'Rectangle', Clutter.KEY_r],
    ['spotlight', 'Spotlight', Clutter.KEY_s],
];
const WIDTH = {pen: 4, highlighter: 22, arrow: 5, rect: 4};
const SPOT_RADIUS = 110;
const SPOT_DIM = 0.6;

export class Drawing {
    constructor(extensionPath) {
        this._path = extensionPath;
        this._strokes = [];
        this._current = null;
        this._tool = 'pen';
        this._color = 0;
        this._pointer = null;
        this._canvas = null;    // the drawings, shown while drawing and after
        this._input = null;     // catches the mouse and keys while drawing
        this._grab = null;
        this._toolbarShown = true;
    }

    get drawing() {
        return !!this._grab;
    }

    toggle() {
        if (this.drawing)
            this.stop(true);
        else
            this.start();
    }

    start() {
        if (this.drawing)
            return;
        this._ensureCanvas();
        this._input = new St.Widget({reactive: true, x: 0, y: 0,
            width: global.stage.width, height: global.stage.height});
        this._toolbar = this._buildToolbar();
        this._toolbar.visible = this._toolbarShown;
        this._input.add_child(this._toolbar);
        Main.uiGroup.add_child(this._input);
        this._grab = Main.pushModal(this._input);
        if (!this._grab) {
            this._input.destroy();
            this._input = null;
            return;
        }
        global.stage.set_key_focus(this._input);
        this._input.connect('button-press-event', (_a, e) => this._press(e));
        this._input.connect('motion-event', (_a, e) => this._motion(e));
        this._input.connect('button-release-event', (_a, e) => this._release(e));
        this._input.connect('key-press-event', (_a, e) => this._key(e));
        setCursor(this._input, 'CROSSHAIR');
        if (this._toolbar.set_cursor_type)      // GNOME 50: a normal pointer over the toolbar
            setCursor(this._toolbar, 'DEFAULT');
        this._placeToolbar();
        this._updateToolbar();
    }

    // keep: leave the drawings on screen (back to using the apps).
    stop(keep = false) {
        if (this._grab) {
            Main.popModal(this._grab);
            this._grab = null;
        }
        if (this._input)
            setCursor(this._input, 'DEFAULT');
        this._input?.destroy();
        this._input = null;
        this._toolbar = null;
        this._current = null;
        this._pointer = null;           // the spotlight goes with drawing
        if (!keep || !this._strokes.length)
            this.clear();
        else
            this._canvas?.queue_repaint();
    }

    clear() {
        this._strokes = [];
        this._current = null;
        if (this.drawing) {
            this._canvas?.queue_repaint();
            return;
        }
        this._canvas?.destroy();
        this._canvas = null;
    }

    destroy() {
        this.stop(false);
        this.clear();
    }

    // ---- input ------------------------------------------------------------------------

    _inToolbar(event) {
        const actor = global.stage.get_event_actor(event);
        return !!(this._toolbar && actor && this._toolbar.contains(actor));
    }

    _press(event) {
        if (this._inToolbar(event) || event.get_button() !== 1)
            return Clutter.EVENT_PROPAGATE;
        if (this._tool === 'spotlight')
            return Clutter.EVENT_STOP;
        const [x, y] = event.get_coords();
        this._current = {tool: this._tool, color: COLORS[this._color][1], points: [[x, y]]};
        this._fadeToolbar(true);
        return Clutter.EVENT_STOP;
    }

    _motion(event) {
        const [x, y] = event.get_coords();
        if (this._tool === 'spotlight') {
            this._pointer = [x, y];
            this._canvas.queue_repaint();
            return Clutter.EVENT_PROPAGATE;
        }
        if (!this._current)
            return Clutter.EVENT_PROPAGATE;
        const pts = this._current.points;
        if (this._current.tool === 'pen' || this._current.tool === 'highlighter') {
            const [lx, ly] = pts[pts.length - 1];
            if (Math.hypot(x - lx, y - ly) >= 1.5)
                pts.push([x, y]);
        } else {
            pts[1] = [x, y];           // arrow and rectangle: start and end
        }
        this._canvas.queue_repaint();
        return Clutter.EVENT_STOP;
    }

    _release(event) {
        if (!this._current || event.get_button() !== 1)
            return Clutter.EVENT_PROPAGATE;
        const s = this._current;
        this._current = null;
        this._fadeToolbar(false);
        // A click without a drag leaves a dot with the pen, nothing otherwise.
        if (s.points.length > 1 || s.tool === 'pen')
            this._strokes.push(s);
        this._canvas.queue_repaint();
        return Clutter.EVENT_STOP;
    }

    _key(event) {
        const sym = event.get_key_symbol();
        const ctrl = (event.get_state() & Clutter.ModifierType.CONTROL_MASK) !== 0;
        if (matches(event, shortcutFor('framewise-draw'))) {
            this.stop(true);
        } else if (sym === Clutter.KEY_Escape) {
            this.stop(false);
        } else if (ctrl && (sym === Clutter.KEY_z || sym === Clutter.KEY_Z)) {
            this._undo();
        } else if (sym === Clutter.KEY_Delete || sym === Clutter.KEY_BackSpace) {
            this.clear();
        } else if (sym === Clutter.KEY_Tab) {
            this._toolbarShown = !this._toolbarShown;
            if (this._toolbar)
                this._toolbar.visible = this._toolbarShown;
        } else if (sym >= Clutter.KEY_1 && sym <= Clutter.KEY_5) {
            this._setColor(sym - Clutter.KEY_1);
        } else {
            const tool = TOOLS.find(t => t[2] === sym || t[2] === sym + 32);
            if (!tool)
                return Clutter.EVENT_PROPAGATE;
            this._setTool(tool[0]);
        }
        return Clutter.EVENT_STOP;
    }

    _undo() {
        this._strokes.pop();
        this._canvas?.queue_repaint();
    }

    _setTool(tool) {
        this._tool = tool;
        if (tool !== 'spotlight')
            this._pointer = null;
        else
            this._pointer = global.get_pointer().slice(0, 2);
        this._canvas?.queue_repaint();
        this._updateToolbar();
    }

    _setColor(i) {
        this._color = i;
        this._updateToolbar();
    }

    // ---- drawing ----------------------------------------------------------------------

    _ensureCanvas() {
        if (this._canvas)
            return;
        this._canvas = new St.DrawingArea({x: 0, y: 0, width: global.stage.width,
            height: global.stage.height, reactive: false});
        this._canvas.connect('repaint', area => this._repaint(area));
        Main.uiGroup.add_child(this._canvas);
    }

    _repaint(area) {
        const cr = area.get_context();
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineJoin(Cairo.LineJoin.ROUND);
        for (const s of this._strokes)
            drawStroke(cr, s);
        if (this._current)
            drawStroke(cr, this._current);
        if (this._pointer) {
            const [w, h] = area.get_surface_size();
            const [px, py] = this._pointer;
            cr.setFillRule(Cairo.FillRule.EVEN_ODD);
            cr.setSourceRGBA(0, 0, 0, SPOT_DIM);
            cr.rectangle(0, 0, w, h);
            cr.arc(px, py, SPOT_RADIUS, 0, 2 * Math.PI);
            cr.fill();
        }
        cr.$dispose();
    }

    // ---- toolbar ----------------------------------------------------------------------

    _icon(name) {
        return Gio.icon_new_for_string(`${this._path}/icons/framewise-draw-${name}-symbolic.svg`);
    }

    _buildToolbar() {
        const bar = new St.BoxLayout({style_class: 'framewise-draw-bar', reactive: true});
        this._toolButtons = {};
        for (const [id, label, key] of TOOLS) {
            const button = new St.Button({style_class: 'framewise-draw-tool', can_focus: false,
                child: new St.Icon({gicon: this._icon(id), icon_size: 16}),
                accessible_name: `${label} (${String.fromCharCode(key).toUpperCase()})`});
            button.connect('clicked', () => this._setTool(id));
            bar.add_child(button);
            this._toolButtons[id] = button;
        }
        bar.add_child(new St.Widget({style_class: 'framewise-draw-sep'}));
        this._colorButtons = COLORS.map(([hex], i) => {
            const dot = new St.Widget({style_class: 'framewise-draw-dot', style: `background-color: ${hex};`});
            const button = new St.Button({style_class: 'framewise-draw-color', can_focus: false, child: dot,
                accessible_name: `Colour ${i + 1}`});
            button.connect('clicked', () => this._setColor(i));
            bar.add_child(button);
            return button;
        });
        bar.add_child(new St.Widget({style_class: 'framewise-draw-sep'}));
        const undo = new St.Button({style_class: 'framewise-draw-tool', can_focus: false,
            child: new St.Icon({icon_name: 'edit-undo-symbolic', icon_size: 16}), accessible_name: 'Undo'});
        undo.connect('clicked', () => this._undo());
        bar.add_child(undo);
        const clear = new St.Button({style_class: 'framewise-draw-tool', can_focus: false,
            child: new St.Icon({icon_name: 'edit-clear-all-symbolic', icon_size: 16}), accessible_name: 'Clear'});
        clear.connect('clicked', () => this.clear());
        bar.add_child(clear);
        const done = new St.Button({style_class: 'framewise-draw-done', can_focus: false, label: 'Done'});
        done.connect('clicked', () => this.stop(true));
        bar.add_child(done);
        const keys = prettyKeys(shortcutFor('framewise-draw'));
        bar.add_child(new St.Label({style_class: 'framewise-draw-hint', y_align: Clutter.ActorAlign.CENTER,
            text: `Esc: clear and close${keys ? `  ·  ${keys}: keep and close` : ''}  ·  Tab: hide`}));
        bar.connect('notify::width', () => this._placeToolbar());
        return bar;
    }

    _placeToolbar() {
        const monitor = Main.layoutManager.primaryMonitor;
        if (!monitor || !this._toolbar)
            return;
        this._toolbar.set_position(Math.round(monitor.x + (monitor.width - this._toolbar.width) / 2),
            monitor.y + Main.panel.height + 10);
    }

    _updateToolbar() {
        if (!this._toolbar)
            return;
        for (const [id, button] of Object.entries(this._toolButtons)) {
            if (id === this._tool)
                button.add_style_class_name('active');
            else
                button.remove_style_class_name('active');
        }
        this._colorButtons.forEach((b, i) => {
            if (i === this._color)
                b.add_style_class_name('active');
            else
                b.remove_style_class_name('active');
        });
    }

    // Out of the way while a line is being drawn (it may pass under it).
    _fadeToolbar(fade) {
        this._toolbar?.ease({opacity: fade ? 60 : 255, duration: 150});
    }
}

// The pointer's shape over an actor: per actor since GNOME 50, for the
// whole screen before (Meta.Cursor).
function setCursor(actor, name) {
    if (actor?.set_cursor_type && Clutter.CursorType)
        actor.set_cursor_type(Clutter.CursorType[name]);
    else if (Meta.Cursor)
        global.display.set_cursor(Meta.Cursor[name]);
}

// ---- strokes ----------------------------------------------------------------------------

export function drawStroke(cr, s) {
    const [r, g, b] = s.color;
    const pts = s.points;
    if (s.tool === 'highlighter') {
        cr.setSourceRGBA(r, g, b, 0.35);
        cr.setLineWidth(WIDTH.highlighter);
        cr.setLineCap(Cairo.LineCap.BUTT);
        smoothPath(cr, pts);
        cr.stroke();
        cr.setLineCap(Cairo.LineCap.ROUND);
        return;
    }
    // A thin dark edge keeps light colours visible on light pages.
    const passes = [[0, 0, 0, 0.35, 2], [r, g, b, 1, 0]];
    for (const [pr, pg, pb, pa, extra] of passes) {
        cr.setSourceRGBA(pr, pg, pb, pa);
        cr.setLineWidth(WIDTH[s.tool] + extra);
        if (s.tool === 'pen') {
            if (pts.length === 1) {
                cr.arc(pts[0][0], pts[0][1], (WIDTH.pen + extra) / 2, 0, 2 * Math.PI);
                cr.fill();
                continue;
            }
            smoothPath(cr, pts);
            cr.stroke();
        } else if (s.tool === 'rect') {
            if (pts.length < 2)
                continue;
            const [[x0, y0], [x1, y1]] = pts;
            roundedRect(cr, Math.min(x0, x1), Math.min(y0, y1), Math.abs(x1 - x0), Math.abs(y1 - y0), 8);
            cr.stroke();
        } else if (s.tool === 'arrow') {
            if (pts.length < 2)
                continue;
            arrow(cr, pts[0], pts[1], WIDTH.arrow + extra);
        }
    }
}

// Through the midpoints between samples: smooth curves, no corners.
function smoothPath(cr, pts) {
    cr.moveTo(pts[0][0], pts[0][1]);
    if (pts.length < 3) {
        for (const [x, y] of pts.slice(1))
            cr.lineTo(x, y);
        return;
    }
    for (let i = 1; i < pts.length - 1; i++) {
        const [x, y] = pts[i];
        const [nx, ny] = pts[i + 1];
        const mx = (x + nx) / 2, my = (y + ny) / 2;
        // Quadratic through (x, y) as a cubic (cairo has no quadratic curve).
        const [cx, cy] = cr.getCurrentPoint();
        cr.curveTo(cx + 2 / 3 * (x - cx), cy + 2 / 3 * (y - cy),
            mx + 2 / 3 * (x - mx), my + 2 / 3 * (y - my), mx, my);
    }
    const [lx, ly] = pts[pts.length - 1];
    cr.lineTo(lx, ly);
}

function roundedRect(cr, x, y, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    cr.newSubPath();
    cr.arc(x + w - r, y + r, r, -Math.PI / 2, 0);
    cr.arc(x + w - r, y + h - r, r, 0, Math.PI / 2);
    cr.arc(x + r, y + h - r, r, Math.PI / 2, Math.PI);
    cr.arc(x + r, y + r, r, Math.PI, 3 * Math.PI / 2);
    cr.closePath();
}

function arrow(cr, [x0, y0], [x1, y1], width) {
    const len = Math.hypot(x1 - x0, y1 - y0);
    if (len < 2)
        return;
    const head = Math.min(len * 0.45, 10 + width * 3);
    const angle = Math.atan2(y1 - y0, x1 - x0);
    // The line stops inside the head, so its round end doesn't poke out.
    const bx = x1 - Math.cos(angle) * head * 0.6, by = y1 - Math.sin(angle) * head * 0.6;
    cr.moveTo(x0, y0);
    cr.lineTo(bx, by);
    cr.stroke();
    cr.moveTo(x1, y1);
    cr.lineTo(x1 - head * Math.cos(angle - 0.45), y1 - head * Math.sin(angle - 0.45));
    cr.lineTo(x1 - head * Math.cos(angle + 0.45), y1 - head * Math.sin(angle + 0.45));
    cr.closePath();
    cr.fill();
}
