const Gio = imports.gi.Gio;
const GLib = imports.gi.GLib;
const IFACE = `<node><interface name="org.gnome.Shell.Extensions.LiveDictation">
  <method name="RememberTarget"><arg type="b" direction="out" name="ok"/></method>
  <method name="RestoreTarget"><arg type="b" direction="out" name="ok"/></method>
  <method name="IsTargetFocused"><arg type="b" direction="out" name="ok"/></method>
</interface></node>`;

class Extension {
    constructor() {
        this._target = null;
        this._dbus = null;
        this._nameOwner = 0;
    }

    enable() {
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, '/org/gnome/Shell/Extensions/LiveDictation');
        this._nameOwner = Gio.bus_own_name_on_connection(Gio.DBus.session,
            'org.gnome.Shell.Extensions.LiveDictation', Gio.BusNameOwnerFlags.NONE, null, null);
    }

    disable() {
        if (this._dbus) this._dbus.unexport();
        if (this._nameOwner) Gio.bus_unown_name(this._nameOwner);
        this._dbus = null;
        this._nameOwner = 0;
        this._target = null;
    }

    RememberTarget() {
        const focused = global.display.focus_window;
        if (focused && focused.get_wm_class() !== 'live-dictation') {
            this._target = focused;
            return true;
        }
        return this._target !== null;
    }

    RestoreTarget() {
        if (!this._target || this._target.get_compositor_private() === null)
            return false;
        this._target.activate(global.get_current_time());
        return true;
    }

    IsTargetFocused() {
        return this._target !== null && global.display.focus_window === this._target;
    }
}

function init() { return new Extension(); }
