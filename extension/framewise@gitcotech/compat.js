// Small differences between GNOME versions (46 to 50).

import Clutter from 'gi://Clutter';
import St from 'gi://St';

// A vertical St.BoxLayout: GNOME 48 added "orientation"; 46 and 47 use
// "vertical" (which 48+ still accepts, but warns about).
export function vbox(params = {}) {
    try {
        return new St.BoxLayout({...params, orientation: Clutter.Orientation.VERTICAL});
    } catch (e) {
        return new St.BoxLayout({...params, vertical: true});
    }
}
