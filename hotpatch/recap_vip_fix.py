"""Hotpatch: Fix VIP Cloud Gateway Option 2 resolution in Nuitka frozen binary.

Problem in v6.5.2:
In frozen Nuitka builds, `license_online` is compiled under the package namespace
`AIVideoTranslator.license_online`. Functions in `core/recap/service.py`,
`core/recap/gemini_service.py`, and `ui/recap_dialog.py` attempted `import license_online`
inside function bodies within try/except blocks. Because `sys.modules['license_online']`
did not exist, Python raised ModuleNotFoundError, caught by `except Exception: pass`,
causing VIP status to falsely evaluate as False for Option 2 Cloud Gateway.

This hotpatch:
1. Locates `license_online` from `AIVideoTranslator.license_online` or active namespaces.
2. Injects `sys.modules['license_online'] = lo`, ensuring any subsequent `import license_online`
   in compiled code immediately resolves without error.
3. Sets `_license_online = lo` on `core.recap.service`, `core.recap.gemini_service`, and
   `ui.recap_dialog` if they are already imported.
4. Introspects any currently active `RecapWorkspace` Qt widgets and refreshes provider combo and VIP status.
"""

import sys


def apply_recap_vip_patch():
    # 1. Resolve license_online module
    lo = (
        sys.modules.get('AIVideoTranslator.license_online')
        or sys.modules.get('license_online')
    )
    if lo is None:
        try:
            import AIVideoTranslator.license_online as lo
        except Exception:
            try:
                import license_online as lo
            except Exception:
                lo = None

    if lo is None:
        print("[HotPatch:RecapVIP] Warning: license_online module could not be resolved.")
        return False

    # 2. Register alias in sys.modules so all `import license_online` calls succeed
    sys.modules['license_online'] = lo
    if 'AIVideoTranslator.license_online' not in sys.modules:
        sys.modules['AIVideoTranslator.license_online'] = lo

    # 3. Patch core.recap.service
    for mod_name in ('AIVideoTranslator.core.recap.service', 'core.recap.service'):
        mod = sys.modules.get(mod_name)
        if mod is not None:
            setattr(mod, '_license_online', lo)

    # 4. Patch core.recap.gemini_service
    for mod_name in ('AIVideoTranslator.core.recap.gemini_service', 'core.recap.gemini_service'):
        mod = sys.modules.get(mod_name)
        if mod is not None:
            setattr(mod, '_license_online', lo)

    # 5. Patch ui.recap_dialog
    for mod_name in ('AIVideoTranslator.ui.recap_dialog', 'ui.recap_dialog'):
        mod = sys.modules.get(mod_name)
        if mod is not None:
            setattr(mod, '_license_online', lo)

    # 6. Dynamically update any open RecapWorkspace dialog widgets
    try:
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app:
            allowed = False
            try:
                if hasattr(lo, 'is_ai_providing_allowed'):
                    allowed = bool(lo.is_ai_providing_allowed())
            except Exception:
                pass

            for widget in app.allWidgets():
                if widget.__class__.__name__ == 'RecapWorkspace':
                    try:
                        widget._has_vip = allowed
                        if hasattr(widget, '_rebuild_provider_combo'):
                            widget._rebuild_provider_combo()
                        elif hasattr(widget, '_update_provider_fields_visibility'):
                            widget._update_provider_fields_visibility()
                    except Exception:
                        pass
    except Exception:
        pass

    print("[HotPatch:RecapVIP] Successfully hooked license_online into sys.modules and recap services.")
    return True


# Automatically execute when imported/loaded
apply_recap_vip_patch()
