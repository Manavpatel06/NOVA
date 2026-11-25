// NOVA face: put the "NOVA face" app's window behind every other window, on every
// workspace, so NOVA's live face is the hub's desktop (ADR 0028). The window itself
// is drawn by hub/face/renderer.py; this only moves where GNOME Shell shows it.
import Meta from 'gi://Meta';
import GLib from 'gi://GLib';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as Workspace from 'resource:///org/gnome/shell/ui/workspace.js';
import * as AltTab from 'resource:///org/gnome/shell/ui/altTab.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const TITLE = 'NOVA face';
const APP_IDS = ['io.nova.face', 'renderer.py', 'python3'];

function isFace(win) {
    if (!win || win.get_title() !== TITLE)
        return false;
    const cls = win.get_wm_class() || '';
    const app = win.get_gtk_application_id ? (win.get_gtk_application_id() || '') : '';
    return APP_IDS.includes(app) || APP_IDS.includes(cls) || cls.startsWith('io.nova.face');
}

export default class NovaFaceExtension extends Extension {
    enable() {
        this._moved = new Map();
        this._created = global.display.connect('window-created', (_d, win) => this._watch(win));
        this._monitors = Main.layoutManager.connect('monitors-changed', () => this._fitAll());
        for (const actor of global.get_window_actors())
            this._watch(actor.meta_window);
        this._hideFromSwitchers();
    }

    // The face is the desktop, not an app: keep it out of the overview and Alt+Tab.
    _hideFromSwitchers() {
        this._restore = [];
        const patch = (proto, name, wrap) => {
            if (!proto || typeof proto[name] !== 'function')
                return;
            const orig = proto[name];
            proto[name] = wrap(orig);
            this._restore.push(() => { proto[name] = orig; });
        };
        patch(Workspace.Workspace?.prototype, '_isOverviewWindow', orig => function (win) {
            return isFace(win) ? false : orig.call(this, win);
        });
        patch(AltTab.WindowSwitcherPopup?.prototype, '_getWindowList', orig => function (...args) {
            return orig.apply(this, args).filter(w => !isFace(w));
        });
    }

    disable() {
        if (this._created)
            global.display.disconnect(this._created);
        if (this._monitors)
            Main.layoutManager.disconnect(this._monitors);
        for (const [actor, parent] of this._moved) {
            // give the window back to the normal window stack
            if (actor.get_parent())
                actor.get_parent().remove_child(actor);
            parent.add_child(actor);
        }
        for (const undo of this._restore || [])
            undo();
        this._restore = null;
        this._moved = null;
        this._created = this._monitors = null;
    }

    _watch(win) {
        if (!win)
            return;
        // the title arrives a moment after the window does
        let tries = 0;
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, 100, () => {
            if (!this._moved)
                return GLib.SOURCE_REMOVE;
            if (isFace(win) && win.get_compositor_private()) {
                this._toBackground(win);
                return GLib.SOURCE_REMOVE;
            }
            return ++tries < 50 ? GLib.SOURCE_CONTINUE : GLib.SOURCE_REMOVE;
        });
    }

    _toBackground(win) {
        const actor = win.get_compositor_private();
        if (!actor || this._moved.has(actor))
            return;
        win.stick();                          // on every workspace
        const parent = actor.get_parent();
        parent.remove_child(actor);
        Main.layoutManager._backgroundGroup.add_child(actor);   // above the wallpaper, below every window
        this._moved.set(actor, parent);
        this._fit(win);
        win.connect('unmanaged', () => this._moved && this._moved.delete(actor));
    }

    _fit(win) {
        const m = Main.layoutManager.primaryMonitor;
        if (!m)
            return;
        if (win.is_fullscreen && !win.is_fullscreen())
            win.make_fullscreen();
        win.move_resize_frame(false, m.x, m.y, m.width, m.height);
    }

    _fitAll() {
        if (!this._moved)
            return;
        for (const actor of this._moved.keys())
            this._fit(actor.meta_window);
    }
}
