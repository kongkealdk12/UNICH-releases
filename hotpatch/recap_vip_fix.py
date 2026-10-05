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
4. Monkey-patches `AnalysisOptions.provider` and `RecapService.analyze` to proactively
   populate `gateway_api_key` and `gateway_url` so Option 2 NEVER raises license errors.
5. Introspects any currently active `RecapWorkspace` Qt widgets, updates `_defaults['has_vip'] = True`,
   and refreshes provider combo and VIP status.
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

    # 3. Patch core.recap.service module and class methods
    for mod_name in ('AIVideoTranslator.core.recap.service', 'core.recap.service'):
        mod = sys.modules.get(mod_name)
        if mod is not None:
            setattr(mod, '_license_online', lo)
            
            # Monkey-patch AnalysisOptions.provider to ensure gateway_api_key is always injected
            if hasattr(mod, 'AnalysisOptions') and not getattr(mod.AnalysisOptions.provider, '_recap_vip_patched', False):
                _orig_provider = mod.AnalysisOptions.provider
                
                def _patched_provider(self):
                    cfg = _orig_provider(self)
                    cur_lo = sys.modules.get('AIVideoTranslator.license_online') or sys.modules.get('license_online') or lo
                    if getattr(cfg, 'mode', '') in ('unich_gateway', 'vip_gateway') or not getattr(cfg, 'api_keys', None):
                        if not getattr(cfg, 'gateway_api_key', ''):
                            try:
                                if hasattr(cur_lo, 'is_ai_providing_allowed') and cur_lo.is_ai_providing_allowed():
                                    creds = cur_lo.get_nisay_credentials()
                                    if creds and creds.get('api_key'):
                                        cfg.gateway_api_key = str(creds['api_key']).strip()
                                        cfg.gateway_url = str(creds.get('gateway_url') or 'https://api.nisay.store/v1').strip()
                                        cfg.api_key = cfg.gateway_api_key
                                        cfg.mode = 'unich_gateway'
                            except Exception:
                                pass
                    return cfg
                
                _patched_provider._recap_vip_patched = True
                mod.AnalysisOptions.provider = _patched_provider

            # Monkey-patch RecapService.analyze to guarantee valid credentials before pipeline execution
            if hasattr(mod, 'RecapService') and not getattr(mod.RecapService.analyze, '_recap_vip_patched', False):
                _orig_analyze = mod.RecapService.analyze
                
                def _patched_analyze(self, source_path, options, *args, **kwargs):
                    cur_lo = sys.modules.get('AIVideoTranslator.license_online') or sys.modules.get('license_online') or lo
                    if getattr(options, 'provider_mode', '') in ('unich_gateway', 'vip_gateway') or not getattr(options, 'gemini_api_keys', None):
                        if not getattr(options, 'gateway_api_key', ''):
                            try:
                                if hasattr(cur_lo, 'is_ai_providing_allowed') and cur_lo.is_ai_providing_allowed():
                                    creds = cur_lo.get_nisay_credentials()
                                    if creds and creds.get('api_key'):
                                        options.gateway_api_key = str(creds['api_key']).strip()
                                        options.gateway_url = str(creds.get('gateway_url') or 'https://api.nisay.store/v1').strip()
                                        options.provider_mode = 'unich_gateway'
                            except Exception:
                                pass
                    return _orig_analyze(self, source_path, options, *args, **kwargs)
                
                _patched_analyze._recap_vip_patched = True
                mod.RecapService.analyze = _patched_analyze

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
            
            # Monkey-patch RecapWorkspace._analysis_options to ensure generated options have gateway key
            if hasattr(mod, 'RecapWorkspace') and not getattr(mod.RecapWorkspace._analysis_options, '_recap_vip_patched', False):
                _orig_ao = mod.RecapWorkspace._analysis_options
                
                def _patched_ao(self):
                    opts = _orig_ao(self)
                    cur_lo = sys.modules.get('AIVideoTranslator.license_online') or sys.modules.get('license_online') or lo
                    if getattr(opts, 'provider_mode', '') in ('unich_gateway', 'vip_gateway') or not getattr(opts, 'gemini_api_keys', None):
                        if not getattr(opts, 'gateway_api_key', ''):
                            try:
                                if hasattr(cur_lo, 'is_ai_providing_allowed') and cur_lo.is_ai_providing_allowed():
                                    creds = cur_lo.get_nisay_credentials()
                                    if creds and creds.get('api_key'):
                                        opts.gateway_api_key = str(creds['api_key']).strip()
                                        opts.gateway_url = str(creds.get('gateway_url') or 'https://api.nisay.store/v1').strip()
                                        opts.provider_mode = 'unich_gateway'
                            except Exception:
                                pass
                    return opts
                
                _patched_ao._recap_vip_patched = True
                mod.RecapWorkspace._analysis_options = _patched_ao

    # 6. Dynamically update any currently open RecapWorkspace dialog widgets
    try:
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app:
            allowed = False
            try:
                cur_lo = sys.modules.get('AIVideoTranslator.license_online') or sys.modules.get('license_online') or lo
                if hasattr(cur_lo, 'is_ai_providing_allowed'):
                    allowed = bool(cur_lo.is_ai_providing_allowed())
            except Exception:
                pass

            for widget in app.allWidgets():
                if widget.__class__.__name__ == 'RecapWorkspace':
                    try:
                        if hasattr(widget, '_defaults') and isinstance(widget._defaults, dict):
                            widget._defaults['has_vip'] = allowed
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
